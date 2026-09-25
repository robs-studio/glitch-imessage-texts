"""Conversation-days, the person-day fold, the ledger key, the topic, and the stored transcript.

Everything this suite holds still is a rule whose failure is SILENT.  A person
folded twice writes two lines where there was one thing; a silent group member
earning a day writes somebody else's words onto that silent member's card; a
busy texter's days merged into fewer lines erase days of contact from their card.
None of that raises, so the only thing standing between those bugs and the
member's real people directory is this file.

Fixture-driven throughout: :class:`imchat.Message` is a frozen dataclass, so the
units can be built directly and a rule can be cornered on purpose (a midnight
crossing, a silent participant, a third party opening a group thread, thirty
straight days from one person).  Exactly ONE test reads the member's real
database, and it prints **counts only**.

PRIVACY — the rule this file is written under
----------------------------------------------
**Nothing here may print a message body, a real name or a real phone number** —
not in output, not in a failure message.  Every fixture handle is invented: the
``555-01xx`` range is the reserved fictional block and the domains are
``example.com`` (RFC 2606).  Nothing is ever written into the real
``THREADS_DIR``; every write test aims at a temp directory.

Negative controls
------------------
An invariant sweep that never fires is decoration.  Each sweep lives in a shared
helper, the real tests run it over a real fixture run, and a control class feeds
the same helper a deliberately violating input and asserts it FAILS.  The guard
on the write target has a control of its own: it runs familywall's predicate
beside ours over the same paths and shows that the borrowed one would have waved
the escape through.

``imredact`` is built by another builder in parallel
------------------------------------------------------
This module codes against its published contract (``redact``, ``find_hits``,
``RULE_NAMES``).  When that file is not on disk yet, this suite installs a small
in-test stand-in FIRST, prints one loud line saying so, and runs everything
against it, so the pipeline is still proved end to end.  The moment the real
module lands the stand-in is not used at all and these same tests exercise the
real redactor — the sweeps are written against whatever ``find_hits`` reports,
never against a hard-coded pattern.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own modules first, before anything reachable only because
# imconfig put `.claude/scripts` on sys.path.
# A sorter would put imchat above imconfig and hoist the stdlib above both; the
# order is a sys.path contract, not a style choice, so each group is fenced.
import imconfig  # noqa: E402,F401

# isort: split
import imchat  # noqa: E402
import imcontacts  # noqa: E402

# isort: split
import os  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, date, datetime, timedelta, timezone  # noqa: E402

# ---------------------------------------------------------------------------
# The imredact seam. Installed BEFORE imthreads is imported, and only when the
# real module is genuinely absent.
# ---------------------------------------------------------------------------


def _install_redaction_stand_in() -> bool:
    """Return True when the real ``imredact`` is on disk; install a double if not.

    The double carries the four rule shapes the plan names for the real module —
    a one-time code, a long digit run, ``password is …`` and a card tail — so the
    sweeps and their controls bite exactly as they will against the real thing.
    It is a TEST DOUBLE in ``sys.modules``, never a file: nothing is created in
    the plug-in folder and the real module is never shadowed.
    """
    if (PLUGIN_HOME / "imredact.py").is_file():
        return True

    module = types.ModuleType("imredact")
    rules = (
        ("one_time_code", re.compile(r"(?i)\b(?:code|otp|pin)\b[^0-9\n]{0,20}\d{4,8}\b")),
        ("long_digit_run", re.compile(r"\b\d{7,}\b")),
        ("password_phrase", re.compile(r"(?i)\bpassword\s+is\s+\S+")),
        ("card_tail", re.compile(r"(?i)\bending in \d{4}\b")),
    )

    def find_hits(body):
        if not body:
            return []
        hits = []
        for name, pattern in rules:
            for match in pattern.finditer(body):
                hits.append((name, match.start(), match.end()))
        return sorted(hits, key=lambda hit: (hit[1], hit[2]))

    def redact(body):
        if body is None:
            return None
        spans = []
        for _name, start, end in find_hits(body):
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        out = body
        for start, end in reversed(spans):
            out = out[:start] + "[redacted]" + out[end:]
        return out

    module.RULE_NAMES = tuple(name for name, _ in rules)
    module.find_hits = find_hits
    module.redact = redact
    sys.modules["imredact"] = module
    return False


REAL_REDACT = _install_redaction_stand_in()

import imredact  # noqa: E402
import imthreads  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixture vocabulary.
# ---------------------------------------------------------------------------

#: A fixed offset stands in for the configured zone. Fixed on purpose: the day
#: boundary under test must not move with whatever this machine is set to.
ZONE = timezone(timedelta(hours=-4))

#: Fictional handles (the reserved 555-01xx block; RFC 2606 domains).
ALICE = "+15555550101"
BOB = "+15555550102"
CAROL = "+15555550103"
ME_PHONE = "+15555550199"
ME_MAIL = "me@example.com"
SHORTCODE = "22395"
SHORTCODE_SIX = "262966"  # the longest short code: +1 would push it over the floor
MAILBOX = "someone@example.com"

OWN = frozenset({ME_PHONE, ME_MAIL})

#: Long enough to clear even the original 80-character substance floor, and
#: plainly a real exchange rather than six rounds of "ok".
LONG_A = "Can we move the appointment to four o'clock, or is the morning easier for you?"
LONG_B = "Four works fine for me, I will let the front desk know and see you then."

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


def _raw(moment: datetime) -> int:
    """An aware datetime as Apple nanoseconds — written out, not imported."""
    delta = moment - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def at(hour, minute=0, day=14, month=9, year=2026):
    """A moment in the fixture's configured zone."""
    return datetime(year, month, day, hour, minute, tzinfo=ZONE)


def msg(
    when,
    *,
    chat=1,
    from_me=False,
    handle=ALICE,
    text="hello",
    is_group=False,
    chat_name=None,
):
    """One :class:`imchat.Message`, built directly rather than through SQLite.

    ``handle`` is the COUNTERPART, exactly as :mod:`imchat` documents it: the
    sender on an inbound row and the recipient on an outbound one.  An outbound
    fixture row passes ``handle=None`` when it is modelling the outbound rows that
    record no destination at all.
    """
    return imchat.Message(
        chat_rowid=chat,
        chat_style=imchat.GROUP_STYLE if is_group else imchat.ONE_TO_ONE_STYLE,
        chat_name=chat_name,
        is_group=is_group,
        date_raw=_raw(when),
        dt_local=when,
        is_from_me=from_me,
        handle=handle,
        text=text,
    )


def mine(when, **kw):
    """A message the member sent. No handle, like most outbound rows here."""
    kw.setdefault("handle", None)
    return msg(when, from_me=True, **kw)


def chat_days(messages, own=OWN):
    return imthreads.group(messages, own_handles=own)


def one_chat_day(messages, own=OWN):
    days = chat_days(messages, own)
    assert len(days) == 1, f"expected one chat-day, got {len(days)}"
    return next(iter(days.values()))


def loose_cfg(**over):
    """A config with the floors relaxed, for tests about something else.

    Written out rather than taken from disk so a test never depends on the
    member's real ``config.local.json`` — which does not exist on a fresh clone.
    """
    cfg = dict(imconfig.DEFAULTS)
    cfg.update(
        {
            "substance_min_turns": 1,
            "substance_min_senders": 1,
            "substance_min_chars": 0,
            "own_handles": [ME_PHONE, ME_MAIL],
        }
    )
    cfg.update(over)
    return cfg


def shipped_cfg(**over):
    """The shipped floors, with own_handles filled in so nothing refuses."""
    cfg = dict(imconfig.DEFAULTS)
    cfg["own_handles"] = [ME_PHONE, ME_MAIL]
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------------------
# Shared assertions — every one is reused by a negative control.
# ---------------------------------------------------------------------------

_REACTION_VERBS = ("Liked", "Loved", "Emphasized", "Laughed at", "Disliked", "Questioned")

#: Reaction wording, in both quote spellings and in more than one language.
#: Every real English reaction on the Mac this was measured on opens with a CURLY
#: quote (see tests/test_invariant_sweep.py's docstring), so a straight-quote-only
#: list would pass while leaking; tapbacks can arrive in other languages too (German
#: shown here).
REACTION_MARKERS = (
    *(f'{verb} "' for verb in _REACTION_VERBS),
    *(f"{verb} “" for verb in _REACTION_VERBS),
    "Gefällt",
    "ein Herz",
)

#: The emoji tapback, ``Reacted 👍 to “…”``. Anchored at the start of the BODY,
#: because "reacted … to" is ordinary English anywhere else in a sentence.
REACTED_WITH_EMOJI = re.compile(r"^Reacted .{1,16} to [\"“]")

#: ``HH:MM  Who: body`` — the whole shape of a transcript line.
LINE_SHAPE = re.compile(r"^(\d{2}:\d{2})  (.+?): (.*)$")

#: The engine's card-pointer parser, copied verbatim from ``people_index._LINK_RE``.
#: Copied rather than imported so this suite needs no engine import; a test pins
#: the copy to the engine's source text so it cannot drift silently.
ENGINE_LINK_RE = re.compile(r"\(\s*→\s*([^)]+)\)")


def _engine_reads_back(topic, link):
    """``(link, summary)`` as the engine's projector reads them off a stamped line.

    The line is ``people_stamp._interaction_line``'s shape with this plug-in's
    source — ``- <date> — conversation (<direction>): <topic> (→ <link>)`` — with
    the leading ``- `` already stripped, as ``people_index._interaction_blocks``
    does.  Link and summary are then taken exactly as
    ``people_index._insert_interactions`` takes them.
    """
    rest = f"{topic} (→ {link})"
    block = f"2026-09-14 — conversation (in): {rest}"
    found = ENGINE_LINK_RE.search(block)
    link_read = found.group(1).strip() if found else None
    summary = ENGINE_LINK_RE.sub("", rest).strip().rstrip(".")
    return link_read, summary


def _assert_no_reaction_body(case, lines, where):
    """No line carries reaction wording. ONE helper; a control proves it bites.

    The emoji form is matched against the BODY (the line's shape is parsed
    first), since it is anchored at the body's start; a line that is not in the
    transcript shape is checked whole, and the single-line sweep fails it anyway.
    """
    for index, line in enumerate(lines):
        shaped = LINE_SHAPE.match(line)
        body = shaped.group(3) if shaped else line
        for marker in REACTION_MARKERS:
            if marker in line:
                case.fail(
                    f"{where}: line {index} carries reaction wording ({marker!r}). "
                    "A tapback is an annotation Apple generated, not anything "
                    "anybody typed, and quoting one onto a card invents a "
                    "sentence the person never said."
                )
        if REACTED_WITH_EMOJI.match(body):
            case.fail(
                f"{where}: line {index} carries an emoji tapback ('Reacted … to'). "
                "Apple generated it; nobody typed it."
            )


def _assert_no_redaction_hit(case, lines, where):
    """No rendered body still trips ``imredact``. ONE helper; a control proves it."""
    for index, line in enumerate(lines):
        match = LINE_SHAPE.match(line)
        if match is None:
            case.fail(
                f"{where}: line {index} is not a rendered message line, so the "
                "sweep cannot tell a body from a speaker. Shape expected: "
                "'HH:MM  Who: body'."
            )
        hits = imredact.find_hits(match.group(3))
        if hits:
            case.fail(
                f"{where}: line {index}'s body still trips {sorted({h[0] for h in hits})} "
                "after redaction, so a secret reached the disk."
            )


def _assert_single_line_rendering(case, text, where):
    """Every message is exactly one line. ONE helper; a control proves it bites."""
    if text and not text.endswith("\n"):
        case.fail(f"{where}: the transcript does not end with a newline.")
    for index, line in enumerate(text.splitlines()):
        for bad in ("\r", "\n", " ", " "):
            if bad in line:
                case.fail(
                    f"{where}: line {index} still contains a line break "
                    f"({bad!r}), so one message became two lines on disk."
                )
        if not LINE_SHAPE.match(line):
            case.fail(f"{where}: line {index} is not a rendered message line.")


def _assert_owner_only(case, path, where):
    """Mode 0600, where the OS means it. ONE helper; a control proves it bites."""
    if os.name == "nt":
        case.skipTest("POSIX modes are advisory on Windows")
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    case.assertEqual(
        mode,
        0o600,
        f"{where}: {Path(path).name} is mode {mode:04o}, not 0600 — a transcript "
        "of the member's private messages is readable by every account on the Mac.",
    )


def _assert_member_never_a_counterpart(case, units, own, where):
    """The member appears nowhere as somebody to file against. ONE helper."""
    for unit in units:
        identifier = getattr(unit, "identifier", None)
        if identifier is not None and identifier in own:
            case.fail(
                f"{where}: a person-day was minted for one of the member's own "
                "handles, so the member's own words would be stamped onto the "
                "member's own card as if a stranger had said them."
            )
        for role, values in (("speakers", unit.speakers), ("counterparts", unit.counterparts)):
            for value in values:
                if value in own:
                    case.fail(
                        f"{where}: one of the member's own handles appears in "
                        f"{role}, so the member is being treated as the other side."
                    )


def _a_body_that_trips_redaction(case):
    """A body the ACTIVE ``imredact`` really flags — asked, never assumed.

    Written against ``find_hits`` rather than a hard-coded pattern so the control
    keeps biting whichever redactor is in play.  A redactor that flags none of
    these is itself the finding, so this fails rather than skipping.
    """
    candidates = (
        "your code is 483920",
        "call me on 5551234567",
        "the password is hunter2",
        "the card ending in 4242",
    )
    for candidate in candidates:
        if imredact.find_hits(candidate):
            return candidate
    case.fail(
        "imredact flagged none of the control bodies, so the redaction sweep "
        "would pass over anything. Either RULE_NAMES is empty or find_hits is "
        f"not reporting (rules: {getattr(imredact, 'RULE_NAMES', None)})."
    )


def _a_long_digit_secret(case):
    """A body the ACTIVE ``imredact`` flags AND that carries a long digit run.

    The truncation test needs a secret that can be cut in half, which a short
    code-shaped hit cannot demonstrate.  Asked of ``find_hits`` rather than
    assumed, so it keeps working against the real module.
    """
    for candidate in ("call me on 5551234567", "the card number is 4242424242424242"):
        if imredact.find_hits(candidate) and re.search(r"\d{8,}", candidate):
            return candidate
    case.fail(
        "imredact flags no long digit run, so the redact-before-truncate rule "
        "cannot be demonstrated against it."
    )


class ThreadsTempCase(unittest.TestCase):
    """A temp threads directory. The real THREADS_DIR is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.threads = Path(self._tmp.name) / "threads"

    def written_files(self):
        return sorted(p for p in self.threads.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# 1. Grouping — the day comes from each message's own local date.
# ---------------------------------------------------------------------------


class TestGroupSplitsAtMidnight(unittest.TestCase):
    """A conversation that crosses midnight is two conversation-days."""

    def test_one_conversation_across_midnight_becomes_two_chat_days(self):
        late = at(23, 40, day=14)
        early = at(0, 20, day=15)
        days = chat_days(
            [msg(late, text=LONG_A), msg(early, from_me=True, handle=None, text=LONG_B)]
        )
        self.assertEqual(
            sorted(days),
            [(1, date(2026, 9, 14)), (1, date(2026, 9, 15))],
            "a conversation running past midnight was filed whole, so words "
            "spoken on the 15th would land on the 14th's line.",
        )
        self.assertEqual([len(d.messages) for _, d in sorted(days.items())], [1, 1])

    def test_the_day_follows_the_configured_zone_not_utc(self):
        """22:00 in a UTC-4 zone is the NEXT day in UTC; the local date wins."""
        moment = at(22, 0, day=14)
        self.assertEqual(moment.astimezone(UTC).date(), date(2026, 9, 15))
        unit = one_chat_day([msg(moment)])
        self.assertEqual(
            unit.day,
            date(2026, 9, 14),
            "the chat-day was keyed off UTC rather than the message's own "
            "configured-zone date, so every late-evening conversation moves a day.",
        )

    def test_two_conversations_on_one_date_stay_apart(self):
        days = chat_days([msg(at(9), chat=1), msg(at(10), chat=2, handle=BOB)])
        self.assertEqual(len(days), 2)


# ---------------------------------------------------------------------------
# 2. The person-day fold (decision 1).
# ---------------------------------------------------------------------------


class TestFoldDedupesAPersonAcrossConversations(unittest.TestCase):
    """One person in a 1:1 and two groups on one date is ONE unit, not three."""

    def setUp(self):
        self.messages = [
            msg(at(9), chat=1, handle=ALICE, text=LONG_A),
            mine(at(9, 5), chat=1, text=LONG_B),
            msg(at(11), chat=2, handle=ALICE, is_group=True, chat_name="Garden Group", text=LONG_A),
            msg(
                at(11, 2), chat=2, handle=BOB, is_group=True, chat_name="Garden Group", text=LONG_B
            ),
            msg(at(14), chat=3, handle=ALICE, is_group=True, text=LONG_A),
            mine(at(14, 3), chat=3, is_group=True, text=LONG_B),
        ]

    def test_three_conversations_fold_to_one_person_day(self):
        units = imthreads.fold_to_person_days(chat_days(self.messages))
        alice_days = [key for key in units if key[0] == ALICE]
        self.assertEqual(
            alice_days,
            [(ALICE, date(2026, 9, 14))],
            "the same person on the same date produced more than one unit; "
            "/recall only reads the twenty most recent interactions off a card, "
            "so every duplicate pushes a real memory off the end.",
        )
        unit = units[(ALICE, date(2026, 9, 14))]
        self.assertEqual(len(unit.chat_days), 3)
        self.assertEqual(unit.message_count, 6)

    def test_the_other_group_member_gets_their_own_unit(self):
        units = imthreads.fold_to_person_days(chat_days(self.messages))
        self.assertIn((BOB, date(2026, 9, 14)), units)
        self.assertEqual(len(units[(BOB, date(2026, 9, 14))].chat_days), 1)

    def test_a_person_days_turns_are_its_conversations_turns_added_up(self):
        units = imthreads.fold_to_person_days(chat_days(self.messages))
        unit = units[(ALICE, date(2026, 9, 14))]
        per_chat = sum(len(c.turns) for c in unit.chat_days)
        self.assertEqual(
            len(unit.turns),
            per_chat,
            "turns were recomputed across the merged stream, which invents "
            "turn-taking between two conversations that never spoke to each other.",
        )


class TestASilentParticipantEarnsNothing(unittest.TestCase):
    """The speaker rule: a person earns a day only if THEY sent a turn in it."""

    def setUp(self):
        # Carol is in the group chat and says nothing all day.
        self.messages = [
            msg(at(9), chat=5, handle=ALICE, is_group=True, chat_name="Garden Group", text=LONG_A),
            msg(at(9, 4), chat=5, handle=BOB, is_group=True, chat_name="Garden Group", text=LONG_B),
            mine(at(9, 9), chat=5, is_group=True, chat_name="Garden Group", text=LONG_A),
        ]
        self.units = imthreads.fold_to_person_days(chat_days(self.messages))

    def test_the_silent_member_gets_no_unit(self):
        self.assertNotIn(
            (CAROL, date(2026, 9, 14)),
            self.units,
            "a person who said nothing earned a conversation-day. Their card "
            "would read 'they reached out', with somebody else's sentence "
            "quoted as the topic.",
        )

    def test_the_people_who_spoke_do_get_one(self):
        self.assertEqual(
            sorted(key[0] for key in self.units), sorted([ALICE, BOB])
        )

    def test_a_silent_participant_is_not_even_a_counterpart_here(self):
        """Carol never appears on a row, so she is in no list this pipeline keeps."""
        unit = one_chat_day(self.messages)
        self.assertNotIn(CAROL, unit.speakers)
        self.assertNotIn(CAROL, unit.counterparts)

    def test_a_named_recipient_who_says_nothing_earns_nothing(self):
        """The reachable shape of a silent participant, and the one that matters.

        ``imchat`` reads no participant table at all, so a group member who never
        sent anything is invisible — with ONE exception: an OUTBOUND row names
        its recipient in ``handle``, which makes that person a **counterpart**
        without their ever having spoken.  Folding on counterparts rather than on
        speakers would mint a day for them out of the member's own message.
        """
        messages = [
            msg(at(9), chat=7, handle=ALICE, is_group=True, chat_name="Garden Group", text=LONG_A),
            # Outbound, and it names Bob as the recipient. Bob says nothing all day.
            msg(at(9, 5), chat=7, from_me=True, handle=BOB, is_group=True,
                chat_name="Garden Group", text=LONG_B),
        ]
        unit = one_chat_day(messages)
        self.assertIn(BOB, unit.counterparts, "the fixture no longer models a counterpart")
        self.assertNotIn(BOB, unit.speakers)

        units = imthreads.fold_to_person_days(chat_days(messages))
        self.assertNotIn(
            (BOB, date(2026, 9, 14)),
            units,
            "somebody who only ever appeared as the recipient of the member's "
            "own message earned a conversation-day, so his card would claim a "
            "conversation that never happened.",
        )
        self.assertEqual(sorted(key[0] for key in units), [ALICE])

    def test_an_unanswered_one_to_one_earns_the_recipient_nothing(self):
        """The same rule in a 1:1, where the outbound handle really is the person."""
        messages = [
            msg(at(9), chat=8, from_me=True, handle=ALICE, text=LONG_A),
            msg(at(9, 5), chat=8, from_me=True, handle=ALICE, text=LONG_B),
        ]
        unit = one_chat_day(messages)
        self.assertEqual(unit.counterparts, (ALICE,))
        self.assertEqual(unit.speakers, ())
        self.assertEqual(imthreads.fold_to_person_days(chat_days(messages)), {})


class TestAnUnknownSenderCountsButEarnsNothing(unittest.TestCase):
    """An inbound row with no handle is a real voice and is nobody's interaction."""

    def setUp(self):
        self.messages = [
            msg(at(9), chat=6, handle=None, text=LONG_A),
            mine(at(9, 2), chat=6, text=LONG_B),
        ]

    def test_it_counts_as_a_distinct_sender(self):
        unit = one_chat_day(self.messages)
        self.assertIn(imthreads.UNKNOWN_SPEAKER, unit.senders)
        self.assertEqual(len(unit.senders), 2)

    def test_it_earns_no_person_day(self):
        self.assertEqual(imthreads.fold_to_person_days(chat_days(self.messages)), {})


# ---------------------------------------------------------------------------
# 3. The substance floor — turns, not message count.
# ---------------------------------------------------------------------------


class TestTheSubstanceFloorCountsTurns(unittest.TestCase):
    def test_six_rounds_of_ok_from_one_person_is_one_turn_and_is_dropped(self):
        messages = [msg(at(9, minute), text="ok") for minute in range(6)]
        unit = one_chat_day(messages)
        self.assertEqual(
            len(unit.turns),
            1,
            "six consecutive messages from one speaker are ONE turn; counting "
            "messages instead would let a monologue clear a floor meant to prove "
            "two people spoke.",
        )
        self.assertFalse(imthreads.is_included(unit, shipped_cfg()))

    def test_a_real_two_sided_exchange_is_kept(self):
        unit = one_chat_day([msg(at(9), text=LONG_A), mine(at(9, 3), text=LONG_B)])
        self.assertEqual(len(unit.turns), 2)
        self.assertEqual(len(unit.senders), 2)
        self.assertTrue(imthreads.is_included(unit, shipped_cfg()))

    def test_an_unanswered_outbound_text_is_not_an_interaction(self):
        unit = one_chat_day([mine(at(9), text=LONG_A), mine(at(9, 1), text=LONG_B)])
        self.assertEqual(len(unit.senders), 1)
        self.assertFalse(
            imthreads.is_included(unit, shipped_cfg()),
            "a message the member sent that nobody answered was logged as an "
            "interaction with that person.",
        )

    def test_the_shipped_floor_keeps_the_short_logistics_exchange(self):
        """The member's decision of 2026-09-20, pinned — with both sides still visible.

        "Can we move to 4pm?" / "Works for me" is two turns from two senders and
        is the plan's own example of the truth worth keeping.  It is 31
        characters, so the originally-specified 80-character floor threw it
        away — the rule contradicting the example written beside it.

        Measured over a year of one real store, the 80-character floor dropped
        about one conversation-day in twenty that a 20-character floor keeps, and
        `last_contacted` is wrong on those cards without them.  The member chose
        the complete contact history, so the shipped default is 20 and this
        exchange now lands.

        BOTH sides stay pinned deliberately: the second assertion is what makes
        the trade legible in the suite rather than something rediscovered later
        on a card that never appeared.
        """
        unit = one_chat_day(
            [msg(at(9), text="Can we move to 4pm?"), mine(at(9, 1), text="Works for me")]
        )
        self.assertEqual(len(unit.turns), 2)
        self.assertEqual(len(unit.senders), 2)
        self.assertTrue(
            imthreads.is_included(unit, shipped_cfg()),
            "the shipped default dropped the short logistics exchange the member chose "
            "to keep.",
        )
        self.assertFalse(
            imthreads.is_included(unit, shipped_cfg(substance_min_chars=80)),
            "the 80-char floor no longer drops it, so the trade this test records is gone.",
        )

    def test_wordless_messages_carry_no_characters(self):
        unit = one_chat_day(
            [msg(at(9), text=""), mine(at(9, 1), text=""), msg(at(9, 2), text="")]
        )
        self.assertEqual(unit.body_chars, 0)
        self.assertFalse(imthreads.is_included(unit, shipped_cfg()))

    def test_an_unreadable_body_neither_raises_nor_counts(self):
        unit = one_chat_day([msg(at(9), text=None), mine(at(9, 1), text=LONG_B)])
        self.assertEqual(unit.body_chars, len(LONG_B))


class TestTheNeverIngestListAndTheShortcodeFloor(unittest.TestCase):
    def test_a_never_ingest_handle_drops_the_whole_conversation(self):
        unit = one_chat_day([msg(at(9), handle=ALICE, text=LONG_A), mine(at(9, 2), text=LONG_B)])
        self.assertTrue(imthreads.is_included(unit, shipped_cfg()))
        self.assertFalse(imthreads.is_included(unit, shipped_cfg(never_ingest=[ALICE])))

    def test_never_ingest_matches_whatever_spelling_the_member_typed(self):
        """The member types 555-555-0101; the database holds +15555550101."""
        unit = one_chat_day([msg(at(9), handle=ALICE, text=LONG_A), mine(at(9, 2), text=LONG_B)])
        self.assertFalse(
            imthreads.is_included(unit, shipped_cfg(never_ingest=["(555) 555-0101"])),
            "the never-ingest list only matched one spelling, so it quietly did "
            "nothing for a member who typed a number the normal way.",
        )

    def test_a_short_code_is_a_bulk_sender_not_a_person(self):
        """End to end through ``canonicalise``, at the boundary that matters.

        Since the member's ruling of 2026-09-24 a bare number gains ``+1``, and
        ``is_shortcode`` counts the digits after a ``+``.  A 5-digit code would
        survive a missing exemption (``+1`` + 5 = 6, still under the floor); a
        6-digit one would not (``+1`` + 6 = 7), so both are checked.
        """
        for code in (SHORTCODE, SHORTCODE_SIX):
            unit = one_chat_day(
                [msg(at(9), handle=code, text=LONG_A), mine(at(9, 2), text=LONG_B)]
            )
            with self.subTest(digits=len(code)):
                self.assertTrue(imthreads.is_shortcode(code))
                self.assertTrue(
                    imthreads.is_shortcode(imcontacts.canonicalise(code)),
                    "the canonical spelling of a short code is no longer a short code, "
                    "so a bank or a 2FA robot would be recorded as a person.",
                )
                self.assertFalse(imthreads.is_included(unit, shipped_cfg()))

    def test_a_real_number_and_an_address_are_never_short_codes(self):
        # The UK number is in Ofcom's reserved drama range (020 7946 0xxx).
        for identifier in (ALICE, MAILBOX, "+442079460123", "5551234567"):
            self.assertFalse(imthreads.is_shortcode(identifier), identifier)


# ---------------------------------------------------------------------------
# 4. Direction — from that person's turns, never the day's first message.
# ---------------------------------------------------------------------------


class TestDirectionFor(unittest.TestCase):
    def test_they_opened(self):
        unit = one_chat_day([msg(at(9), text=LONG_A), mine(at(9, 5), text=LONG_B)])
        self.assertEqual(imthreads.direction_for(unit, ALICE), imthreads.THEY_REACHED_OUT)

    def test_the_member_opened(self):
        unit = one_chat_day([mine(at(9), text=LONG_A), msg(at(9, 5), text=LONG_B)])
        self.assertEqual(imthreads.direction_for(unit, ALICE), imthreads.I_REACHED_OUT)

    def test_both_sides_more_than_once_is_mutual(self):
        unit = one_chat_day(
            [
                msg(at(9), text=LONG_A),
                mine(at(9, 1), text=LONG_B),
                msg(at(9, 2), text=LONG_A),
                mine(at(9, 3), text=LONG_B),
            ]
        )
        self.assertEqual(imthreads.direction_for(unit, ALICE), imthreads.MUTUAL)

    def test_a_group_where_neither_party_spoke_first(self):
        """Carol opens a group thread; the direction between the member and Bob
        is still read from BOB's turns beside the member's, not from Carol's."""
        unit = one_chat_day(
            [
                msg(at(9), handle=CAROL, is_group=True, chat_name="Garden Group", text=LONG_A),
                mine(at(9, 5), chat=1, is_group=True, chat_name="Garden Group", text=LONG_B),
                msg(at(9, 9), handle=BOB, is_group=True, chat_name="Garden Group", text=LONG_A),
            ]
        )
        self.assertEqual(
            imthreads.direction_for(unit, BOB),
            imthreads.I_REACHED_OUT,
            "the direction was taken from whoever spoke first in the group — a "
            "third party — so Bob's card would claim he reached out on a day he "
            "answered.",
        )
        self.assertEqual(imthreads.direction_for(unit, CAROL), imthreads.THEY_REACHED_OUT)

    def test_every_answer_is_in_the_engines_vocabulary(self):
        unit = one_chat_day([msg(at(9), text=LONG_A), mine(at(9, 5), text=LONG_B)])
        for identifier in (ALICE, BOB, CAROL, MAILBOX):
            self.assertIn(imthreads.direction_for(unit, identifier), imthreads.DIRECTIONS)

    def test_a_raw_spelling_of_the_identifier_still_matches(self):
        unit = one_chat_day([msg(at(9), text=LONG_A), mine(at(9, 5), text=LONG_B)])
        self.assertEqual(
            imthreads.direction_for(unit, "(555) 555-0101"), imthreads.THEY_REACHED_OUT
        )


# ---------------------------------------------------------------------------
# 5. The member is never a counterpart, and an empty own_handles refuses.
# ---------------------------------------------------------------------------


class TestTheMemberIsNeverACounterpart(unittest.TestCase):
    """The member's own number arrives as an inbound row, and must still be recognised."""

    def setUp(self):
        self.messages = [
            # Sent from another of the member's own devices: inbound, own handle.
            msg(at(8), chat=1, handle=ME_PHONE, text=LONG_A),
            msg(at(9), chat=1, handle=ALICE, text=LONG_A),
            mine(at(9, 5), chat=1, text=LONG_B),
            msg(at(10), chat=2, handle=ME_MAIL, is_group=True, chat_name="Family", text=LONG_B),
            msg(at(10, 5), chat=2, handle=BOB, is_group=True, chat_name="Family", text=LONG_A),
        ]

    def test_no_unit_anywhere_treats_the_member_as_the_other_side(self):
        days = chat_days(self.messages)
        people = imthreads.fold_to_person_days(days)
        _assert_member_never_a_counterpart(
            self, [*days.values(), *people.values()], OWN, "the fixture run"
        )

    def test_their_inbound_row_is_folded_into_their_own_turn(self):
        unit = chat_days(self.messages)[(1, date(2026, 9, 14))]
        self.assertEqual(unit.turns[0].speaker, imthreads.OWNER)
        self.assertTrue(unit.turns[0].is_from_me)

    def test_is_from_me_alone_is_not_enough(self):
        """Without own_handles the same row becomes a stranger's turn."""
        unit = one_chat_day(self.messages[:3], own=frozenset())
        self.assertIn(
            ME_PHONE,
            unit.speakers,
            "with no own_handles the member's own number was read as somebody "
            "else — which is the whole reason an empty list is a refusal.",
        )


class TestTheOwnHandlesRefusal(unittest.TestCase):
    def test_an_empty_list_refuses_with_the_members_sentence(self):
        with self.assertRaises(imthreads.OwnHandlesRequired) as caught:
            imthreads.owner_handles(dict(imconfig.DEFAULTS))
        self.assertEqual(str(caught.exception), imconfig.REFUSAL_NO_OWN_HANDLES)

    def test_the_entry_point_refuses_before_reading_anything(self):
        with self.assertRaises(imthreads.OwnHandlesRequired):
            imthreads.units_for([msg(at(9))], dict(imconfig.DEFAULTS))

    def test_a_malformed_entry_refuses_rather_than_being_skipped(self):
        for bad in ([""], ["   "], [None], "not-a-list", []):
            with self.assertRaises(imthreads.OwnHandlesRequired):
                imthreads.owner_handles({"own_handles": bad})

    def test_handles_that_carry_no_digits_at_all_refuse(self):
        """``canonicalise`` says 'not a handle', which leaves nothing to match on."""
        with self.assertRaises(imthreads.OwnHandlesRequired):
            imthreads.owner_handles({"own_handles": ["---"]})

    def test_a_good_list_is_canonicalised(self):
        self.assertEqual(
            imthreads.owner_handles({"own_handles": ["(555) 555-0199", "Me@Example.com"]}),
            frozenset({ME_PHONE, ME_MAIL}),
        )


# ---------------------------------------------------------------------------
# 6. The ledger key, and one line per person per day.
# ---------------------------------------------------------------------------


class TestTheLedgerKey(unittest.TestCase):
    """``<identifier>|day|<YYYY-MM-DD>`` — three parts, the middle one always ``day``."""

    DAY = date(2026, 9, 14)

    def test_the_key_is_identifier_day_date(self):
        self.assertEqual(
            imthreads.ledger_key(ALICE, imthreads.DAY_GRAIN, self.DAY),
            f"{ALICE}|day|2026-09-14",
            "the ledger key changed shape. Every key already written, and every "
            "module coded against it, reads the three-part day shape.",
        )

    def test_a_string_period_builds_the_same_key_as_a_date(self):
        self.assertEqual(
            imthreads.ledger_key(ALICE, imthreads.DAY_GRAIN, "2026-09-14"),
            imthreads.ledger_key(ALICE, imthreads.DAY_GRAIN, self.DAY),
        )

    def test_keys_round_trip(self):
        for identifier in (ALICE, MAILBOX, "with|a|pipe"):
            key = imthreads.ledger_key(identifier, imthreads.DAY_GRAIN, self.DAY)
            self.assertEqual(
                imthreads.parse_ledger_key(key), (identifier, imthreads.DAY_GRAIN, self.DAY)
            )

    def test_junk_parses_to_none_rather_than_raising(self):
        for junk in (
            "",
            "nope",
            "a|b",
            f"{ALICE}|fortnight|2026-09-14",
            f"{ALICE}|week|2026-09-14",
            f"{ALICE}|day|nope",
            7,
        ):
            self.assertIsNone(imthreads.parse_ledger_key(junk))

    def test_any_grain_but_day_is_refused_at_the_source(self):
        """A key nothing ever looks up is a day recorded where no reader finds it."""
        for grain in ("week", "fortnight", "", "DAY"):
            with self.subTest(grain=grain), self.assertRaises(ValueError):
                imthreads.ledger_key(ALICE, grain, self.DAY)

    def test_day_is_the_only_grain(self):
        self.assertEqual(imthreads.GRAINS, frozenset({imthreads.DAY_GRAIN}))
        self.assertEqual(imthreads.DAY_GRAIN, "day")


def _ledger(days, identifier=ALICE):
    """A ledger-shaped dict of day stamps for one identifier."""
    return {
        "stamped": {
            imthreads.ledger_key(identifier, imthreads.DAY_GRAIN, day): {
                "grain": imthreads.DAY_GRAIN
            }
            for day in days
        }
    }


class TestStampedPeriods(unittest.TestCase):
    """What the ledger says was already stamped — every shape a caller may pass."""

    DAYS = [date(2026, 9, d) for d in (1, 5, 9)]

    def test_a_ledger_dict_reads_back_every_day(self):
        self.assertEqual(
            sorted(imthreads.stamped_periods(ALICE, _ledger(self.DAYS))),
            [(imthreads.DAY_GRAIN, day) for day in self.DAYS],
        )

    def test_an_empty_ledger_is_nothing_stamped(self):
        for empty in (None, {}, {"stamped": {}}, []):
            with self.subTest(ledger=empty):
                self.assertEqual(imthreads.stamped_periods(ALICE, empty), [])

    def test_another_persons_history_is_not_counted(self):
        self.assertEqual(imthreads.stamped_periods(BOB, _ledger(self.DAYS)), [])

    def test_a_legacy_two_part_key_is_still_read_as_stamped(self):
        """A ledger written before the grain joined the key must not re-stamp a year."""
        ledger = {"stamped": {f"{ALICE}|2026-09-0{d}": {} for d in (1, 5, 9)}}
        self.assertEqual(
            sorted(imthreads.stamped_periods(ALICE, ledger)),
            [(imthreads.DAY_GRAIN, day) for day in self.DAYS],
        )

    def test_a_bare_iterable_of_keys_is_accepted(self):
        keys = [imthreads.ledger_key(ALICE, imthreads.DAY_GRAIN, day) for day in self.DAYS]
        self.assertEqual(
            sorted(imthreads.stamped_periods(ALICE, keys)),
            [(imthreads.DAY_GRAIN, day) for day in self.DAYS],
        )

    def test_an_object_with_a_stamped_attribute_is_accepted(self):
        ledger = types.SimpleNamespace(stamped=_ledger(self.DAYS)["stamped"])
        self.assertEqual(len(imthreads.stamped_periods(ALICE, ledger)), len(self.DAYS))


class TestOneLinePerPersonPerDay(unittest.TestCase):
    """The member's ruling, 2026-09-24: however often someone texts, every day they
    texted is a line.

    There is no weekly fold.  A person who texts on thirty straight days gets
    thirty lines on their card, under thirty distinct day keys — the busiest
    people in the member's life are exactly the ones whose days must not be
    merged away.
    """

    DAYS = 30
    FIRST = date(2026, 9, 1)

    def setUp(self):
        self.days = [self.FIRST + timedelta(days=offset) for offset in range(self.DAYS)]
        messages = []
        for day in self.days:
            messages.append(msg(at(9, day=day.day, month=day.month), handle=ALICE, text=LONG_A))
            messages.append(mine(at(9, 5, day=day.day, month=day.month), text=LONG_B))
        self.included, self.people = imthreads.units_for(messages, shipped_cfg())

    def test_thirty_consecutive_days_are_thirty_lines(self):
        alice_days = sorted(day for identifier, day in self.people if identifier == ALICE)
        self.assertEqual(
            alice_days,
            self.days,
            f"thirty straight days of texts produced {len(alice_days)} lines, not "
            "thirty. Every day a person texted is its own line on their card, "
            "however often they text.",
        )
        self.assertEqual(len(self.included), self.DAYS)

    def test_thirty_days_are_thirty_distinct_day_keys(self):
        keys = [
            imthreads.ledger_key(identifier, imthreads.DAY_GRAIN, day)
            for identifier, day in self.people
        ]
        self.assertEqual(
            len(set(keys)),
            self.DAYS,
            "two of the thirty days share a ledger key, so the second one would "
            "read as already stamped and silently never land.",
        )
        for key in keys:
            identifier, grain, day = imthreads.parse_ledger_key(key)
            self.assertEqual(identifier, ALICE)
            self.assertEqual(grain, imthreads.DAY_GRAIN)
            self.assertIn(day, self.days)

    def test_no_day_reads_as_already_stamped_when_walked_in_order(self):
        """The ledger a real run builds as it goes never absorbs a later day."""
        stamped = {}
        for identifier, day in sorted(self.people, key=lambda key: key[1]):
            key = imthreads.ledger_key(identifier, imthreads.DAY_GRAIN, day)
            self.assertNotIn(
                (imthreads.DAY_GRAIN, day),
                imthreads.stamped_periods(identifier, {"stamped": stamped}),
                f"day {day.day} read as already stamped before it was written.",
            )
            stamped[key] = {"grain": imthreads.DAY_GRAIN}
        self.assertEqual(len(stamped), self.DAYS)


# ---------------------------------------------------------------------------
# 7. Sanitisation, labels and topics.
# ---------------------------------------------------------------------------


class TestSanitiseBody(unittest.TestCase):
    def test_every_line_break_collapses_to_a_single_space(self):
        for token in ("\r\n", "\r", "\n", " ", " "):
            self.assertEqual(imthreads.sanitise_body(f"a{token}b"), "a b", repr(token))

    def test_a_paragraph_break_is_one_space_not_three(self):
        self.assertEqual(imthreads.sanitise_body("a\n\n\nb"), "a b")

    def test_none_and_whitespace_are_empty_rather_than_an_error(self):
        self.assertEqual(imthreads.sanitise_body(None), "")
        self.assertEqual(imthreads.sanitise_body("   \n\t "), "")

    def test_it_is_idempotent(self):
        for value in ("a\nb", "  x  ", "a\r\n\r\nb", ""):
            once = imthreads.sanitise_body(value)
            self.assertEqual(imthreads.sanitise_body(once), once)


class TestLabelsAndTopics(unittest.TestCase):
    def test_a_bracketed_contacts_label_slugs_clean(self):
        unit = one_chat_day([msg(at(9), handle=ALICE, text=LONG_A), mine(at(9, 1), text=LONG_B)])
        slug = imthreads.slug_for(unit, {ALICE: "Mom (cell)"})
        self.assertEqual(slug, "mom-cell")
        for bad in ("(", ")", "/", ":", " "):
            self.assertNotIn(bad, slug)

    def test_an_unnamed_group_gets_a_stable_label(self):
        unit = one_chat_day(
            [
                msg(at(9), handle=ALICE, is_group=True, text=LONG_A),
                msg(at(9, 1), handle=BOB, is_group=True, text=LONG_B),
            ]
        )
        self.assertEqual(imthreads.slug_for(unit, {}), imthreads.UNNAMED_GROUP_LABEL)

    def test_an_unnamed_counterpart_falls_back_to_the_canonical_handle(self):
        unit = one_chat_day([msg(at(9), handle=ALICE, text=LONG_A), mine(at(9, 1), text=LONG_B)])
        self.assertEqual(imthreads.slug_for(unit, {}), imcontacts.slugify_label(ALICE))

    def test_a_one_to_one_topic_carries_the_opener(self):
        unit = one_chat_day([msg(at(9), text=LONG_A), mine(at(9, 1), text=LONG_B)])
        topic = imthreads.topic_for(unit)
        self.assertTrue(topic.startswith("Texts (2): "))
        self.assertIn(LONG_A[:20], topic)

    def test_a_long_opener_is_capped(self):
        unit = one_chat_day([msg(at(9), text="x" * 400), mine(at(9, 1), text=LONG_B)])
        opener = imthreads.topic_for(unit).split(": ", 1)[1]
        self.assertLessEqual(len(opener), imthreads.TOPIC_OPENER_MAX)

    def test_an_opener_is_redacted_before_it_is_truncated(self):
        """The cut is aimed at the middle of the secret, which is the whole risk.

        Truncating first cuts a long number in half; the half that is left no
        longer matches the pattern, so redaction passes over it and a partial
        secret reaches the card.  The filler is sized so exactly four characters
        of the number fall off the end — enough to stop the pattern matching,
        and still six digits of a phone number in plain sight.
        """
        secret = _a_long_digit_secret(self)
        # Sweep the cut ACROSS the number rather than guessing one offset: where
        # exactly a wrong implementation would slice is an implementation detail,
        # and a single offset can land on a cut that happens to stay matchable.
        for drop in range(3, 9):
            body = "a" * (imthreads.TOPIC_OPENER_MAX - 1 - len(secret) + drop) + secret
            unit = one_chat_day([msg(at(9), text=body), mine(at(9, 1), text=LONG_B)])
            opener = imthreads.topic_for(unit).split(": ", 1)[1]

            self.assertLessEqual(len(opener), imthreads.TOPIC_OPENER_MAX)
            self.assertNotRegex(
                opener,
                r"\d{4,}",
                f"(cut {drop} characters into the number) the opener was "
                "truncated before it was redacted, so the cut broke the pattern "
                "and a fragment of the number survived onto the card.",
            )
            self.assertEqual(imredact.find_hits(opener), [])

    def test_a_group_topic_quotes_no_body_at_all(self):
        body = "the exact words somebody else typed in the group chat"
        unit = one_chat_day(
            [
                msg(at(9), handle=CAROL, is_group=True, chat_name="Garden Group", text=body),
                msg(at(9, 1), handle=BOB, is_group=True, chat_name="Garden Group", text=LONG_B),
            ]
        )
        topic = imthreads.topic_for(unit)
        self.assertEqual(topic, "Group texts in Garden Group (2 messages)")
        for fragment in (body, LONG_B, body[:12]):
            self.assertNotIn(
                fragment,
                topic,
                "a group topic quoted a message body, so a third party's words "
                "would be attributed to whoever the line was stamped onto.",
            )

    def test_a_topic_is_always_one_line(self):
        unit = one_chat_day(
            [msg(at(9), text="first\nsecond\r\nthird"), mine(at(9, 1), text=LONG_B)]
        )
        self.assertNotIn("\n", imthreads.topic_for(unit))

    #: A link of the exact shape the plug-in writes onto a card.
    CARD_LINK = imthreads.THREAD_LINK_PREFIX + "/2026/2026-09-14-mom-cell-1.txt"

    def test_a_closing_bracket_in_a_body_never_breaks_the_card_link(self):
        """A ``)`` somebody typed reaches the topic, and must never reach the link.

        The engine's pointer parser stops at the first ``)``, but it anchors on
        ``(→``, and the topic sits BEFORE the pointer on the card line, so a
        bracket in the body cannot cut the link short.  Proved through
        ``topic_for`` itself and the engine's own parser: the link is read back
        whole and the summary is exactly the topic.  ``topic_for`` keeps the
        body's brackets, and does not need to strip them; the one sequence that
        CAN hijack the pointer, ``(→``, is refused at the stamp by
        ``imspine.stamp`` (the control below shows why).
        """
        for body in ("Sounds good :)", "call me (after 5)", ") first thing", "ok (", "((nested))"):
            unit = one_chat_day([msg(at(9), text=body), mine(at(9, 1), text=LONG_B)])
            topic = imthreads.topic_for(unit)
            with self.subTest(body=body):
                self.assertIn(body, topic, "the opener is the body's own words, brackets and all")
                link, summary = _engine_reads_back(topic, self.CARD_LINK)
                self.assertEqual(
                    link,
                    self.CARD_LINK,
                    "a bracket in the body cut the card's link short; the card would "
                    "point at a file that does not exist.",
                )
                self.assertEqual(
                    summary,
                    topic.rstrip("."),
                    "a bracket in the body moved the boundary between summary and "
                    "link, so part of one leaked into the other on the card.",
                )

    def test_the_card_link_check_bites_on_a_body_carrying_the_pointer_marker(self):
        """The negative control: ``(→`` in a topic really does hijack the link."""
        unit = one_chat_day(
            [msg(at(9), text="see (→ elsewhere) today"), mine(at(9, 1), text=LONG_B)]
        )
        link, _summary = _engine_reads_back(imthreads.topic_for(unit), self.CARD_LINK)
        self.assertNotEqual(
            link,
            self.CARD_LINK,
            "the engine parser read the right link even past a planted (→, so the "
            "bracket check above proves nothing; re-read people_index._LINK_RE.",
        )

    def test_the_copied_pointer_parser_is_still_the_engines(self):
        """``ENGINE_LINK_RE`` is a copy; a copy that drifted would prove nothing."""
        source = (imconfig.SCRIPTS_DIR / "people_index.py").read_text(encoding="utf-8")
        self.assertIn(
            f'_LINK_RE = re.compile(r"{ENGINE_LINK_RE.pattern}")',
            source,
            "people_index._LINK_RE no longer reads as copied here; re-copy it so the "
            "card-link tests test the engine's real parser.",
        )


# ---------------------------------------------------------------------------
# 8. The stored transcript.
# ---------------------------------------------------------------------------


class TestWriteThread(ThreadsTempCase):
    def setUp(self):
        super().setUp()
        self.messages = [
            msg(at(9), handle=ALICE, text=LONG_A),
            mine(at(9, 5), text=LONG_B),
        ]
        self.unit = one_chat_day(self.messages)

    def test_it_lands_in_the_year_folder_with_the_rowid_suffix(self):
        path = imthreads.write_thread(self.unit, {ALICE: "Mom (cell)"}, self.threads)
        self.assertEqual(path.parent.name, "2026")
        self.assertEqual(path.name, "2026-09-14-mom-cell-1.txt")
        self.assertTrue(path.is_file())

    def test_it_is_owner_only(self):
        path = imthreads.write_thread(self.unit, {}, self.threads)
        _assert_owner_only(self, path, "the transcript")

    def test_the_tightening_helper_really_tightens(self):
        """``mkstemp`` already makes 0600, so the repair path needs its own proof.

        Without this, a change that dropped the ``chmod`` entirely would still
        pass every other mode assertion in this file — the atomic replace hands
        the temp file's own mode to the target — and the repair of a file left
        loose by an earlier tool would be gone unnoticed.
        """
        if os.name == "nt":
            self.skipTest("POSIX modes are advisory on Windows")
        loose = Path(self._tmp.name) / "loose.txt"
        loose.write_text("x", encoding="utf-8")
        os.chmod(loose, 0o644)
        imthreads._chmod_600(loose)
        _assert_owner_only(self, loose, "a file left loose by an earlier tool")

    def test_replacing_a_loose_file_leaves_it_owner_only(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are advisory on Windows")
        path = imthreads.write_thread(self.unit, {}, self.threads)
        os.chmod(path, 0o644)
        again = imthreads.write_thread(self.unit, {}, self.threads)
        self.assertEqual(again, path)
        _assert_owner_only(self, again, "a rewritten transcript")

    def test_it_renders_one_message_per_line(self):
        path = imthreads.write_thread(self.unit, {ALICE: "Mom (cell)"}, self.threads)
        text = path.read_text(encoding="utf-8")
        _assert_single_line_rendering(self, text, "the transcript")
        self.assertEqual(len(text.splitlines()), 2)
        self.assertIn("Me: ", text.splitlines()[1])

    def test_a_body_full_of_newlines_still_renders_as_one_line(self):
        unit = one_chat_day(
            [msg(at(9), text="one\ntwo\r\nthree" + "!" * 70), mine(at(9, 1), text=LONG_B)]
        )
        path = imthreads.write_thread(unit, {}, self.threads)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 2)
        _assert_single_line_rendering(self, text, "a multi-line body")

    def test_a_contacts_name_carrying_a_newline_cannot_split_a_line(self):
        path = imthreads.write_thread(self.unit, {ALICE: "Mom\n(cell)"}, self.threads)
        _assert_single_line_rendering(self, path.read_text(encoding="utf-8"), "a broken name")

    def test_wordless_and_unreadable_are_kept_apart(self):
        unit = one_chat_day([msg(at(9), text=""), mine(at(9, 1), text=None)])
        path = imthreads.write_thread(unit, {}, self.threads)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertIn(imthreads.NO_WORDS, lines[0])
        self.assertIn(imthreads.UNREADABLE, lines[1])

    def test_a_secret_in_a_body_is_redacted_on_disk(self):
        secret = _a_body_that_trips_redaction(self)
        unit = one_chat_day([msg(at(9), text=secret), mine(at(9, 1), text=LONG_B)])
        path = imthreads.write_thread(unit, {}, self.threads)
        lines = path.read_text(encoding="utf-8").splitlines()
        _assert_no_redaction_hit(self, lines, "a secret body")

    def test_the_link_is_brain_root_relative_and_parser_safe(self):
        path = imthreads.write_thread(self.unit, {ALICE: "Mom (cell)"}, self.threads)
        link = imthreads.thread_link(path, self.threads)
        self.assertEqual(link, "_local/imessage/threads/2026/2026-09-14-mom-cell-1.txt")
        self.assertFalse(
            link.startswith("threads/"),
            "a bare threads/ link is resolved against glitch-mem/Memory/ and "
            "leads nowhere on every card, forever.",
        )
        self.assertNotIn(")", link)
        self.assertNotIn("\\", link)

    def test_two_conversations_sharing_a_label_get_stable_distinct_names(self):
        messages = [
            msg(at(9), chat=41, handle=ALICE, text=LONG_A),
            mine(at(9, 1), chat=41, text=LONG_B),
            msg(at(10), chat=42, handle=BOB, text=LONG_A),
            mine(at(10, 1), chat=42, text=LONG_B),
        ]
        contacts = {ALICE: "Mom (cell)", BOB: "Mom (cell)"}
        units = list(chat_days(messages).values())

        first = {
            u.chat_rowid: imthreads.write_thread(u, contacts, self.threads).name for u in units
        }
        second = {
            u.chat_rowid: imthreads.write_thread(u, contacts, self.threads).name
            for u in reversed(units)
        }
        self.assertEqual(
            first,
            second,
            "two conversations sharing a Contacts label swapped filenames "
            "between runs, so yesterday's link now opens the other thread.",
        )
        self.assertEqual(len(set(first.values())), 2)
        self.assertEqual(
            sorted(first.values()),
            ["2026-09-14-mom-cell-41.txt", "2026-09-14-mom-cell-42.txt"],
        )

    def test_a_person_day_is_refused_because_it_spans_conversations(self):
        person = next(iter(imthreads.fold_to_person_days(chat_days(self.messages)).values()))
        with self.assertRaises(ValueError):
            imthreads.write_thread(person, {}, self.threads)


class TestASerialisationFailureLeavesNothing(ThreadsTempCase):
    """Serialise BEFORE the temp file, so a bad payload costs nothing on disk."""

    class _Exploding:
        is_from_me = False
        handle = ALICE
        text = "never rendered"
        date_raw = 0

        @property
        def dt_local(self):
            raise RuntimeError("this row cannot be rendered")

    def test_no_file_and_no_temp_file_survive(self):
        good = msg(at(9), handle=ALICE, text=LONG_A)
        unit = imthreads.ChatDay(
            chat_rowid=7,
            day=date(2026, 9, 14),
            is_group=False,
            chat_name=None,
            own_handles=OWN,
            messages=(good, self._Exploding()),
            turns=imthreads._turns((good,), OWN),
            counterparts=(ALICE,),
        )
        with self.assertRaises(RuntimeError):
            imthreads.write_thread(unit, {}, self.threads)
        self.assertEqual(
            self.written_files(),
            [],
            "a payload that could not be rendered still left something on disk.",
        )
        self.assertFalse(
            (self.threads / "2026").exists(),
            "an empty year folder was created for a transcript that never existed.",
        )


class TestTheForeignTargetGuard(ThreadsTempCase):
    def setUp(self):
        super().setUp()
        (self.threads / "2026").mkdir(parents=True)
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()

    def test_a_path_inside_the_threads_folder_is_allowed(self):
        target = self.threads / "2026" / "2026-09-14-mom-1.txt"
        self.assertEqual(
            imthreads._refuse_foreign_target(target, self.threads), target.resolve()
        )

    def test_a_path_outside_is_refused(self):
        with self.assertRaises(ValueError):
            imthreads._refuse_foreign_target(self.outside / "x.txt", self.threads)

    def test_a_home_shaped_path_is_refused(self):
        """``~/.ssh/x`` is the shape familywall's predicate would wave through."""
        ssh = Path(self._tmp.name) / ".ssh"
        ssh.mkdir()
        with self.assertRaises(ValueError):
            imthreads._refuse_foreign_target(ssh / "authorized_keys", self.threads)

    def test_a_traversal_out_of_the_folder_is_refused(self):
        with self.assertRaises(ValueError):
            imthreads._refuse_foreign_target(
                self.threads / "2026" / ".." / ".." / "outside" / "x.txt", self.threads
            )

    def test_a_symlink_that_leaves_the_folder_is_refused(self):
        if os.name == "nt":
            self.skipTest("symlink creation needs a privilege on Windows")
        link = self.threads / "escape"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            imthreads._refuse_foreign_target(link / "x.txt", self.threads)

    def test_the_threads_folder_itself_is_not_a_target(self):
        with self.assertRaises(ValueError):
            imthreads._refuse_foreign_target(self.threads, self.threads)

    def test_the_guard_still_holds_when_the_base_is_reached_through_a_symlink(self):
        """macOS temp dirs ARE symlinks; an unresolved compare waves writes through."""
        if os.name == "nt":
            self.skipTest("symlink creation needs a privilege on Windows")
        alias = Path(self._tmp.name) / "alias"
        alias.symlink_to(self.threads, target_is_directory=True)
        target = alias / "2026" / "x.txt"
        self.assertEqual(
            imthreads._refuse_foreign_target(target, self.threads),
            (self.threads / "2026" / "x.txt").resolve(),
        )


class TestFamilywallsPredicateWouldNotHaveProtected(ThreadsTempCase):
    """NEGATIVE CONTROL — why ``session.py:165`` is deliberately not mirrored.

    familywall tests ``resolved.parent == home``: a whitelist of one direct child
    of HOME.  Run it beside ours over the same two paths and it refuses the one
    that is legitimate and permits the one that escapes.
    """

    @staticmethod
    def _familywall_predicate(target: Path, home: Path, only: Path) -> bool:
        resolved = Path(target).resolve()
        return not (resolved.parent == Path(home).resolve() and resolved != Path(only).resolve())

    def test_the_borrowed_predicate_permits_an_escape_that_ours_refuses(self):
        home = Path(self._tmp.name)
        ssh = home / ".ssh"
        ssh.mkdir()
        escape = ssh / "authorized_keys"

        self.assertTrue(
            self._familywall_predicate(escape, home, home / "session.json"),
            "the borrowed predicate was expected to permit this write; if it no "
            "longer does, this control has stopped proving anything.",
        )
        with self.assertRaises(ValueError):
            imthreads._refuse_foreign_target(escape, self.threads)


# ---------------------------------------------------------------------------
# 9. Invariant sweeps over a whole fixture run, and the controls that bite.
# ---------------------------------------------------------------------------


def _sweep_every_thread_file(case, threads_dir, where):
    """Every produced transcript, against every invariant. ONE helper.

    Reactions are not filtered by :mod:`imthreads` — ``imchat`` excludes them by
    ``associated_message_type``, which is the reliable signal across languages —
    so this is a floor over the whole pipeline rather than a second filter.
    """
    files = sorted(p for p in Path(threads_dir).rglob("*") if p.is_file())
    case.assertTrue(files, f"{where}: the run produced no transcript to sweep.")
    for path in files:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        _assert_single_line_rendering(case, text, f"{where}/{path.name}")
        _assert_no_reaction_body(case, lines, f"{where}/{path.name}")
        _assert_no_redaction_hit(case, lines, f"{where}/{path.name}")
        _assert_owner_only(case, path, f"{where}/{path.name}")
    return files


class TestTheInvariantSweepOverAFixtureRun(ThreadsTempCase):
    """A realistic run: a 1:1, two groups, a secret, an attachment, midnight."""

    def setUp(self):
        super().setUp()
        secret = _a_body_that_trips_redaction(self)
        self.messages = [
            msg(at(9), chat=1, handle=ALICE, text=LONG_A),
            mine(at(9, 4), chat=1, text=f"{LONG_B} {secret}"),
            msg(at(9, 9), chat=1, handle=ALICE, text=""),
            msg(
                at(11), chat=2, handle=BOB, is_group=True, chat_name="Garden Group (2026)",
                text=LONG_A,
            ),
            msg(
                at(11, 3), chat=2, handle=CAROL, is_group=True, chat_name="Garden Group (2026)",
                text=LONG_B,
            ),
            mine(at(11, 8), chat=2, is_group=True, chat_name="Garden Group (2026)", text=LONG_A),
            msg(at(23, 50), chat=3, handle=BOB, text=f"{LONG_A}\nsecond paragraph"),
            mine(at(0, 10, day=15), chat=3, text=LONG_B),
        ]
        self.cfg = shipped_cfg()
        self.included, self.people = imthreads.units_for(self.messages, self.cfg)
        self.files = [
            imthreads.write_thread(unit, {ALICE: "Mom (cell)"}, self.threads)
            for unit in self.included.values()
        ]

    def test_every_transcript_holds_every_invariant(self):
        files = _sweep_every_thread_file(self, self.threads, "the fixture run")
        self.assertEqual(len(files), len(self.files))

    def test_the_run_produced_the_units_it_should_have(self):
        self.assertIn((1, date(2026, 9, 14)), self.included)
        self.assertIn((2, date(2026, 9, 14)), self.included)
        self.assertIn((ALICE, date(2026, 9, 14)), self.people)
        self.assertIn((BOB, date(2026, 9, 14)), self.people)

    def test_the_member_is_a_counterpart_nowhere_in_the_run(self):
        _assert_member_never_a_counterpart(
            self, [*self.included.values(), *self.people.values()], OWN, "the fixture run"
        )

    def test_every_link_the_run_would_write_is_parser_safe(self):
        for path in self.files:
            link = imthreads.thread_link(path, self.threads)
            self.assertTrue(link.startswith(imthreads.THREAD_LINK_PREFIX + "/"))
            self.assertNotIn(")", link)
            self.assertNotIn("\\", link)

    def test_every_direction_is_in_the_engines_vocabulary(self):
        for (identifier, _day), unit in self.people.items():
            self.assertIn(imthreads.direction_for(unit, identifier), imthreads.DIRECTIONS)


class TestTheSweepsActuallyBite(ThreadsTempCase):
    """NEGATIVE CONTROLS — each sweep, fed the violation it exists to catch."""

    def _plant(self, body):
        folder = self.threads / "2026"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "2026-09-14-planted-1.txt"
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_a_reaction_body_fails_the_sweep(self):
        self._plant('09:00  Mom: Liked "the sentence somebody else typed"\n')
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_a_curly_quote_reaction_body_fails_the_sweep(self):
        """The form every real English reaction takes on a measured Mac — each verb."""
        for verb in _REACTION_VERBS:
            with self.subTest(verb=verb):
                self._plant(f"09:00  Mom: {verb} “the sentence somebody else typed”\n")
                with self.assertRaises(self.failureException):
                    _sweep_every_thread_file(self, self.threads, "control")

    def test_an_emoji_reaction_body_fails_the_sweep(self):
        self._plant("09:00  Mom: Reacted \U0001F44D to “the sentence somebody else typed”\n")
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_reacted_inside_a_sentence_passes_the_sweep(self):
        """The anchor's control: ordinary English must not be read as a tapback.

        The second body starts a sentence with "Reacted" mid-message, which only
        the start-of-body anchor tells apart from a real tapback.
        """
        for body in (
            "He reacted well to “the news”, thank goodness",
            "The kids were thrilled. Reacted better to “the news” than I expected",
        ):
            with self.subTest(body=body[:20]):
                self._plant(f"09:00  Mom: {body}\n")
                _sweep_every_thread_file(self, self.threads, "control")

    def test_a_german_reaction_body_fails_the_sweep(self):
        self._plant("09:00  Mom: Gefällt der Nachricht\n")
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_an_unredacted_secret_fails_the_sweep(self):
        self._plant(f"09:00  Mom: {_a_body_that_trips_redaction(self)}\n")
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_a_line_break_inside_a_message_fails_the_sweep(self):
        self._plant("09:00  Mom: first\n  second half of the same message\n")
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_a_loose_mode_fails_the_sweep(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are advisory on Windows")
        path = self._plant("09:00  Mom: an ordinary line\n")
        os.chmod(path, 0o644)
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_an_empty_run_fails_the_sweep(self):
        self.threads.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(self.failureException):
            _sweep_every_thread_file(self, self.threads, "control")

    def test_the_member_sweep_bites_when_own_handles_are_missing(self):
        messages = [
            msg(at(8), handle=ME_PHONE, text=LONG_A),
            msg(at(9), handle=ALICE, text=LONG_B),
        ]
        units = chat_days(messages, own=frozenset())
        with self.assertRaises(self.failureException):
            _assert_member_never_a_counterpart(self, units.values(), OWN, "control")


# ---------------------------------------------------------------------------
# 10. The one live aggregate — counts only, never content.
# ---------------------------------------------------------------------------


class TestTheLiveAggregate(unittest.TestCase):
    """Group and fold the last 30 days of the real store. COUNTS ONLY.

    The floor it asserts is deliberately weak — this is an aggregate, not a
    measurement, and the member is texting while it runs.  ``own_handles`` is
    **not** read from the member's config: a placeholder is used so the test is
    the same on every machine and prints nothing private.  Owner turns are then
    recognised by ``is_from_me`` alone, which is what the engine's own reading of
    this Mac's outbound rows amounts to.
    """

    PLACEHOLDER_OWN = "+10000000000"

    @classmethod
    def setUpClass(cls):
        ok, sentence = imchat.has_access()
        if not ok:
            raise unittest.SkipTest(sentence)

    def test_thirty_days_of_the_real_store_group_and_fold(self):
        cfg = shipped_cfg(own_handles=[self.PLACEHOLDER_OWN])
        conn = imchat.connect()
        self.addCleanup(conn.close)

        newest = imchat.date_range(conn)[1]
        if newest is None:
            # Readable but empty (a new Mac, or Messages never used here): nothing to
            # aggregate, which is this machine's state rather than a defect.
            self.skipTest("the Messages store on this Mac holds no messages yet")
        since = newest - timedelta(days=30)

        messages = list(imchat.iter_messages(conn, since, None))
        included, people = imthreads.units_for(messages, cfg)
        all_days = imthreads.group(messages, own_handles={self.PLACEHOLDER_OWN})

        persons = {identifier for identifier, _day in people}
        print(
            f"    live 30 days: {len(messages)} messages · {len(all_days)} chat-days · "
            f"{len(included)} clear the substance floor · {len(people)} person-days · "
            f"{len(persons)} distinct people"
        )
        self.assertGreater(len(messages), 0)
        self.assertGreater(len(all_days), 0)
        self.assertLessEqual(len(included), len(all_days))
        self.assertLessEqual(
            len(people),
            sum(len(unit.speakers) for unit in included.values()),
            "the fold produced more person-days than there were speaking "
            "participants, so it is duplicating rather than deduping.",
        )
        _assert_member_never_a_counterpart(
            self, people.values(), {self.PLACEHOLDER_OWN}, "the live aggregate"
        )


if __name__ == "__main__":
    if not REAL_REDACT:
        print(
            "[imessage] imredact.py is not on disk yet, so this run used the "
            "in-test stand-in for redaction.",
            file=sys.stderr,
        )
    unittest.main(verbosity=2)
