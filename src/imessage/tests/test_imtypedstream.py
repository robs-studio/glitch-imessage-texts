"""The typedstream decoder — the gate on whether this plug-in reads texts at all.

Everything downstream of :mod:`imtypedstream` is worthless if it decodes wrong,
so this suite is built around one gate that cannot be faked: it opens the
member's REAL ``~/Library/Messages/chat.db`` read-only, takes every row that
still carries BOTH a ``text`` column and an ``attributedBody`` blob, decodes the
blob, and demands an exact match against the column.  Apple wrote both; if our
reading of the bytes disagrees with Apple's own string, we are wrong.

Around that gate sit synthetic tests that reach the cases the real data cannot:
every length-prefix width including the ``0x82`` escape (constructed here with a
>64 KB body, because a real database holds few such rows, often none, and any it
holds may fall outside the overlap the gate measures), every malformed shape the never-raises
contract promises to survive, and negative controls that prove the assertions
bite — a corrupted blob must NOT decode to the original, the reference
implementation's three-byte ``0x82`` read must be caught disagreeing with ours,
and ``0x83`` must be REFUSED rather than read as an 8-byte length, because it is
the floating-point tag and not an integer at all.

Privacy: this suite reads the member's real messages.  It prints AGGREGATES
only — counts, percentages and lengths.  The single place a message body could
reach the output is a mismatch example, and those are truncated to 24 characters
and have every digit masked before printing.  With the gate green they never
print at all.

No ``__init__.py``, no ``conftest.py``: run it with
``python -m unittest discover -s tests -t tests``.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's OWN modules come first, before anything reachable only because
# imconfig put `.claude/scripts` on sys.path. This suite imports no engine
# module at all, so the ordering can never come down to test-discovery luck.
import imconfig  # noqa: E402,F401  (imported for the sys.path ordering it asserts)
from imtypedstream import HEADER, OBJECT_REPLACEMENT, clean, extract_text  # noqa: E402

import re  # noqa: E402
import sqlite3  # noqa: E402
import unittest  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic blobs, built from the REAL byte layout.
# ---------------------------------------------------------------------------

# The exact bytes that precede the first `NSString` in a real Messages blob,
# lifted from a real Messages database (about three rows in four were
# byte-identical up to here). Using the real prefix rather than an invented one
# means a synthetic test failure is a real-format failure.
REAL_PREFIX = bytes.fromhex(
    "04 0b 73 74 72 65 61 6d 74 79 70 65 64"  # the header
    "81 e8 03"                                # a 0x81 integer: the version, 1000
    "84 01 40 84 84 84 12"
    "4e 53 41 74 74 72 69 62 75 74 65 64 53 74 72 69 6e 67 00"  # NSAttributedString
    "84 84 08 4e 53 4f 62 6a 65 63 74 00"                       # NSObject
    "85 92 84 84 84 08"                       # ...and the length of "NSString"
)

#: The four bytes the real format puts between `NSString` and the `+` marker.
REAL_FILLER = bytes.fromhex("01 94 84 01")

MARKER = b"+"


def encode_length(length: int, tag: int | None = None) -> bytes:
    """A typedstream length prefix, either at its natural width or a forced one.

    ``tag`` of ``None`` picks the width the format would: one signed byte up to
    127, then ``0x81`` + int16, ``0x82`` + int32, ``0x87`` + int64 — all
    little-endian and signed, which is why 32,767 (not 65,535) is the ``0x81``
    ceiling.  Passing ``tag`` explicitly forces an over-wide encoding, which is
    legal and is the only way to exercise the ``0x87`` branch on a real machine,
    where no message comes anywhere near 2 GB.

    ``0x83`` is deliberately NOT in this table: it is the floating-point tag,
    not an 8-byte integer, and a helper that could emit it would let a test
    "prove" a branch that must not exist.  See
    ``imtypedstream``'s "Why 0x83 is REFUSED and 0x87 is the 8-byte tag".
    """
    widths = {0x81: 2, 0x82: 4, 0x87: 8}
    if tag is None:
        if length <= 0x7F:
            return bytes([length])
        tag = 0x81 if length <= 0x7FFF else (0x82 if length <= 0x7FFFFFFF else 0x87)
    width = widths[tag]
    return bytes([tag]) + length.to_bytes(width, "little", signed=True)


def make_blob(
    body: bytes,
    *,
    tag: int | None = None,
    prefix: bytes = REAL_PREFIX,
    filler: bytes = REAL_FILLER,
    marker: bytes = MARKER,
    declared_length: int | None = None,
) -> bytes:
    """A synthetic ``attributedBody`` carrying ``body``, shaped like a real one.

    Every part is overridable so a malformed-input test can break exactly one
    thing and leave the rest honest — a blob that is wrong in two ways proves
    nothing about which one the decoder caught.  ``declared_length`` lies about
    the body's size, which is how the overrun case is built.
    """
    length = len(body) if declared_length is None else declared_length
    return prefix + b"NSString" + filler + marker + encode_length(length, tag) + body


def masked(text: str | None, limit: int = 24) -> str:
    """A short, digit-masked prefix — the ONLY form real message text may print.

    Digits carry phone numbers, addresses, amounts and dates, so every one
    becomes ``#``.  The truncation is what stops a mismatch report turning into
    a transcript.
    """
    if text is None:
        return "<None>"
    return re.sub(r"\d", "#", text[:limit]).replace("\n", "\\n").replace("\r", "\\r")


# ---------------------------------------------------------------------------
# The real database.
# ---------------------------------------------------------------------------

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"


def open_chat_db():
    """The live Messages database, READ-ONLY, or ``None`` with a reason.

    ``mode=ro`` and never ``immutable=1``: the member is texting while this runs,
    and ``immutable`` tells SQLite it may ignore the WAL, which would hand back a
    stale and possibly torn view of a file that is being written.  Read-only is
    the honest read of a live database.  Never a copy either — a copy is a
    different file and proves nothing about the one the plug-in will read.
    """
    if not CHAT_DB.is_file():
        return None, f"{CHAT_DB} does not exist (this is a Mac-only database)"
    try:
        con = sqlite3.connect(f"{CHAT_DB.as_uri()}?mode=ro", uri=True)
        con.execute("select count(*) from message limit 1").fetchone()
    except (sqlite3.Error, OSError) as exc:
        return None, (
            f"{CHAT_DB} could not be opened read-only ({exc.__class__.__name__}: {exc}). "
            "Full Disk Access for this process is what grants it."
        )
    return con, ""


# ---------------------------------------------------------------------------
# 1. The length prefix — every width the format can use.
# ---------------------------------------------------------------------------


class TestLengthPrefixWidths(unittest.TestCase):
    """Each length-marker width decodes to exactly the bytes that were encoded."""

    def test_the_fixture_itself_is_sound(self):
        """A synthetic blob that was malformed by accident would prove nothing."""
        self.assertTrue(REAL_PREFIX.startswith(HEADER))
        self.assertNotIn(
            MARKER,
            REAL_PREFIX + b"NSString" + REAL_FILLER,
            "the fixture's own prefix contains a '+', so the decoder would find "
            "THAT marker instead of the body's and every test here would be "
            "measuring the wrong thing",
        )

    def test_single_byte_length(self):
        body = "Can we move to 4pm?".encode("utf-8")
        self.assertLessEqual(len(body), 0x7F)
        blob = make_blob(body)
        self.assertEqual(blob[blob.find(MARKER) + 1], len(body))  # no tag byte
        self.assertEqual(extract_text(blob), body.decode())

    def test_zero_length_body_is_the_empty_string_not_none(self):
        """An empty NSString is a successful read of nothing, not a failure."""
        self.assertEqual(extract_text(make_blob(b"")), "")

    def test_boundary_at_127_and_128(self):
        """127 is the last single-byte length; 128 must escape to 0x81."""
        short = b"a" * 127
        long = b"a" * 128
        self.assertEqual(make_blob(short)[make_blob(short).find(MARKER) + 1], 127)
        self.assertEqual(make_blob(long)[make_blob(long).find(MARKER) + 1], 0x81)
        self.assertEqual(extract_text(make_blob(short)), short.decode())
        self.assertEqual(extract_text(make_blob(long)), long.decode())

    def test_0x81_two_byte_length(self):
        body = ("x" * 5000).encode("utf-8")
        blob = make_blob(body)
        self.assertEqual(blob[blob.find(MARKER) + 1], 0x81)
        self.assertEqual(extract_text(blob), body.decode())

    def test_0x81_at_its_signed_ceiling(self):
        """32,767 is the largest 0x81 length; 32,768 must escalate to 0x82."""
        at = b"y" * 0x7FFF
        over = b"y" * 0x8000
        self.assertEqual(make_blob(at)[make_blob(at).find(MARKER) + 1], 0x81)
        self.assertEqual(make_blob(over)[make_blob(over).find(MARKER) + 1], 0x82)
        self.assertEqual(extract_text(make_blob(at)), at.decode())
        self.assertEqual(extract_text(make_blob(over)), over.decode())

    def test_0x82_four_byte_length_on_a_body_over_64kb(self):
        """The case the brief singles out: a >64 KB body, which only 0x82 can carry.

        70,000 exceeds an unsigned int16 too, so no reading of ``0x81`` could
        express it. This is the branch the reference implementation gets wrong.
        """
        body = b"z" * 70_000
        blob = make_blob(body)
        self.assertEqual(blob[blob.find(MARKER) + 1], 0x82)
        decoded = extract_text(blob)
        self.assertEqual(len(decoded), 70_000)
        self.assertEqual(decoded, body.decode())
        self.assertFalse(
            decoded.startswith("\x00"),
            "a leading NUL is the signature of a three-byte 0x82 read: the length "
            "survives (its top byte is zero) but the cursor lands one byte early",
        )

    def test_0x87_eight_byte_length(self):
        """Forced over-wide, because no real message is 2 GB long.

        ``0x87``, not ``0x83``, is the 8-byte integer indicator — per the cited
        blog's "Further Discoveries" and per ``python-typedstream``'s constants.
        The brief's table said ``0x83``; it is wrong, and the next test pins why.
        """
        body = "a forced 0x87".encode("utf-8")
        blob = make_blob(body, tag=0x87)
        self.assertEqual(blob[blob.find(MARKER) + 1], 0x87)
        self.assertEqual(extract_text(blob), body.decode())

    def test_0x83_is_refused_because_it_is_the_floating_point_tag(self):
        """0x83 must NEVER be read as a length. It is a live control tag.

        Two independent sources say 0x83 introduces a floating-point value:
        the cited blog ("0x83 is not the 8-byte integer indicator; it introduces
        a floating-point value") and ``python-typedstream``
        (``_TAG_FLOATING_POINT = 0x83``).  A float cannot be a string length, so
        the honest answer is to refuse the blob rather than swallow eight bytes
        of object graph and return whatever happens to follow.
        """
        for tag, meaning in (
            (0x83, "floating point"),
            (0x84, "new object/string"),
            (0x85, "nil"),
            (0x86, "end of object"),
        ):
            with self.subTest(tag=hex(tag), meaning=meaning):
                blob = (
                    REAL_PREFIX + b"NSString" + REAL_FILLER + MARKER
                    + bytes([tag]) + b"\x0c\x00\x00\x00\x00\x00\x00\x00"
                    + b"swallowed!!!"
                )
                self.assertIsNone(
                    extract_text(blob),
                    f"{hex(tag)} ({meaning}) was consumed as a length prefix. It is "
                    "a control tag, not an integer, and reading it as one is how a "
                    "decoder returns plausible rubbish instead of saying no.",
                )

    def test_forced_wide_encodings_all_agree(self):
        body = "same body, three widths".encode("utf-8")
        results = {t: extract_text(make_blob(body, tag=t)) for t in (0x81, 0x82, 0x87)}
        self.assertEqual(set(results.values()), {body.decode()}, results)

    def test_multibyte_utf8_is_decoded_by_bytes_not_characters(self):
        """The length is a BYTE count; counting characters would truncate."""
        body = "café ☕ 家族".encode("utf-8")
        self.assertGreater(len(body), len(body.decode()))
        self.assertEqual(extract_text(make_blob(body)), body.decode())

    def test_bytearray_and_memoryview_are_accepted(self):
        body = b"from a buffer"
        blob = make_blob(body)
        self.assertEqual(extract_text(bytearray(blob)), body.decode())
        self.assertEqual(extract_text(memoryview(blob)), body.decode())


# ---------------------------------------------------------------------------
# 2. Malformed input — the never-raises contract.
# ---------------------------------------------------------------------------


class TestMalformedInputNeverRaises(unittest.TestCase):
    """Every broken shape returns None (or a replaced string). Nothing raises.

    One malformed row in a run over tens of thousands of rows must cost one
    skipped message, never the run, so each case here asserts BOTH halves: no
    exception, and the right answer.
    """

    def _no_raise(self, blob):
        try:
            return extract_text(blob)
        except Exception as exc:  # noqa: BLE001 — proving the contract
            self.fail(f"extract_text raised {exc.__class__.__name__}: {exc}")

    def test_none(self):
        self.assertIsNone(self._no_raise(None))

    def test_empty_bytes(self):
        self.assertIsNone(self._no_raise(b""))

    def test_truncated_header(self):
        self.assertIsNone(self._no_raise(HEADER[:6]))

    def test_wrong_header_entirely(self):
        """An NSKeyedArchiver plist starts bplist00 — it must be refused, not guessed."""
        self.assertIsNone(self._no_raise(b"bplist00" + b"\x00" * 64))

    def test_right_shape_but_header_byte_flipped(self):
        blob = bytearray(make_blob(b"hello"))
        blob[1] ^= 0xFF
        self.assertIsNone(self._no_raise(bytes(blob)))

    def test_header_present_but_no_nsstring(self):
        self.assertIsNone(self._no_raise(HEADER + b"\x84\x84\x08NSObject\x00"))

    def test_nsstring_present_but_no_marker_after_it(self):
        self.assertIsNone(self._no_raise(HEADER + b"....NSString\x01\x94\x84\x01"))

    def test_marker_is_the_very_last_byte(self):
        self.assertIsNone(self._no_raise(HEADER + b"....NSString" + REAL_FILLER + MARKER))

    def test_wide_length_prefix_is_truncated(self):
        """0x81 promises two more bytes; give it one."""
        for tag, short in ((0x81, 1), (0x82, 3), (0x87, 7)):
            with self.subTest(tag=hex(tag)):
                blob = (
                    REAL_PREFIX + b"NSString" + REAL_FILLER + MARKER
                    + bytes([tag]) + b"\x01" * short
                )
                self.assertIsNone(self._no_raise(blob))

    def test_length_runs_past_the_end_of_the_buffer(self):
        """Refused, not truncated: the front of a message must not pass as one."""
        blob = make_blob(b"only twelve", declared_length=9999)
        self.assertIsNone(self._no_raise(blob))

    def test_length_overruns_by_exactly_one_byte(self):
        blob = make_blob(b"abcdef", declared_length=7)
        self.assertIsNone(self._no_raise(blob))

    def test_length_that_exactly_fills_the_buffer_is_accepted(self):
        """The boundary the overrun check must NOT be off by one on."""
        self.assertEqual(extract_text(make_blob(b"abcdef")), "abcdef")

    def test_negative_length(self):
        blob = (
            REAL_PREFIX + b"NSString" + REAL_FILLER + MARKER
            + bytes([0x81]) + (-5).to_bytes(2, "little", signed=True)
            + b"whatever"
        )
        self.assertIsNone(self._no_raise(blob))

    def test_single_byte_marker_in_the_signed_negative_range(self):
        """0x80-0xFF as a bare marker is a negative length; refuse it."""
        blob = REAL_PREFIX + b"NSString" + REAL_FILLER + MARKER + b"\xff" + b"body"
        self.assertIsNone(self._no_raise(blob))

    def test_invalid_utf8_is_replaced_not_raised(self):
        """A mojibake character is still a message; losing the row is worse."""
        body = b"before \xff\xfe after"
        decoded = self._no_raise(make_blob(body))
        self.assertIsNotNone(decoded)
        self.assertIn("\ufffd", decoded)
        self.assertTrue(decoded.startswith("before "))
        self.assertTrue(decoded.endswith(" after"))

    def test_fuzzing_truncations_of_a_real_shaped_blob_never_raise(self):
        """Every prefix of a valid blob — the shape a half-written row has."""
        blob = make_blob("a message with 0x81 length ".encode() * 20)
        for cut in range(0, len(blob), 7):
            with self.subTest(cut=cut):
                self._no_raise(blob[:cut])

    def test_fuzzing_single_byte_corruptions_never_raise(self):
        blob = make_blob(b"a short message")
        for i in range(len(blob)):
            for xor in (0xFF, 0x01, 0x80):
                mutated = bytearray(blob)
                mutated[i] ^= xor
                self._no_raise(bytes(mutated))


# ---------------------------------------------------------------------------
# 3. clean() — and the None / "" distinction.
# ---------------------------------------------------------------------------


class TestClean(unittest.TestCase):
    """U+FFFC is stripped, and the two empty-ish answers stay distinct."""

    def test_none_stays_none_because_it_means_failure(self):
        self.assertIsNone(clean(None))

    def test_attachment_only_body_cleans_to_empty_string_not_none(self):
        """A photo with no caption. Ordinary, not an error."""
        result = clean(OBJECT_REPLACEMENT)
        self.assertIsNotNone(
            result,
            "an attachment-only body must NOT come back as None: None means the "
            "bytes were unreadable, and a caller that conflates the two drops "
            "every photo-only message",
        )
        self.assertEqual(result, "")

    def test_attachment_with_trailing_whitespace_still_cleans_to_empty(self):
        """Without the trim, this would be ' ' and read as a message with content."""
        self.assertEqual(clean(OBJECT_REPLACEMENT + " "), "")
        self.assertEqual(clean(" " + OBJECT_REPLACEMENT + "\n"), "")

    def test_caption_keeps_its_words_and_loses_the_placeholder(self):
        self.assertEqual(clean(OBJECT_REPLACEMENT + "look at this"), "look at this")

    def test_placeholder_in_the_middle_is_removed(self):
        self.assertEqual(clean("before" + OBJECT_REPLACEMENT + "after"), "beforeafter")

    def test_several_placeholders(self):
        self.assertEqual(clean(OBJECT_REPLACEMENT * 4 + "three photos"), "three photos")

    def test_ordinary_text_is_untouched(self):
        for text in ("Can we move to 4pm?", "café ☕", "line one\nline two"):
            with self.subTest(text=masked(text)):
                self.assertEqual(clean(text), text)

    def test_empty_string_in_empty_string_out(self):
        self.assertEqual(clean(""), "")

    def test_the_replacement_character_is_not_confused_with_the_placeholder(self):
        """U+FFFD (bad UTF-8) is content; U+FFFC (an attachment) is not."""
        self.assertEqual(clean("a\ufffdb"), "a\ufffdb")

    def test_clean_never_raises(self):
        # A lone surrogate is a legal Python str that no codec will encode; it is
        # the nastiest thing errors="replace" can hand clean(), so it belongs here.
        for value in (None, "", OBJECT_REPLACEMENT, "\x00", "\ud800", "�" * 500):
            with self.subTest(value=repr(value)[:20]):
                try:
                    clean(value)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"clean raised {exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------------------
# 4. Negative controls — proof the assertions bite.
# ---------------------------------------------------------------------------


class TestNegativeControls(unittest.TestCase):
    """If these passed, every green above would be meaningless."""

    def test_a_corrupted_blob_does_not_decode_to_the_original(self):
        """The control the gate rests on: a broken blob must not look intact."""
        body = "the original message body, long enough to matter".encode("utf-8")
        blob = make_blob(body)
        self.assertEqual(extract_text(blob), body.decode())

        broken = 0
        for i in range(len(REAL_PREFIX) + 8, len(blob) - len(body) + 4):
            mutated = bytearray(blob)
            mutated[i] ^= 0xFF
            if extract_text(bytes(mutated)) != body.decode():
                broken += 1
        self.assertGreater(
            broken,
            0,
            "corrupting the header, the class name, the marker or the length "
            "changed NOTHING about the decoded result — this decoder is not "
            "reading those bytes at all, so the ground-truth gate proves nothing",
        )

    def test_corrupting_the_body_itself_changes_the_result(self):
        body = b"the original message body"
        mutated = bytearray(make_blob(body))
        mutated[-1] ^= 0x20
        self.assertNotEqual(extract_text(bytes(mutated)), body.decode())

    def test_the_reference_three_byte_0x82_read_is_caught_disagreeing(self):
        """The reference implementation's bug, reproduced and pinned.

        The reference implementation (the one-off measuring script this build
        started from) reads THREE bytes after ``0x82`` and advances three.  The
        length survives (its top byte is zero on any realistic message) but the
        cursor lands one byte early.  This test is the
        negative control for the whole 0x82 resolution: if the two readings ever
        agree, the conflict was imaginary and this module's four is unjustified.
        """

        def reference_body(ab: bytes) -> str | None:
            """Verbatim from the reference measuring script's body reader."""
            if not ab:
                return None
            i = ab.find(b"NSString")
            if i < 0:
                return None
            j = ab.find(b"+", i)
            if j < 0:
                return None
            p = j + 1
            if p >= len(ab):
                return None
            n = ab[p]
            p += 1
            if n == 0x81:
                n = int.from_bytes(ab[p:p + 2], "little")
                p += 2
            elif n == 0x82:
                n = int.from_bytes(ab[p:p + 3], "little")
                p += 3
            elif n >= 0x80:
                return None
            return ab[p:p + n].decode("utf-8", "replace")

        body = b"z" * 70_000
        blob = make_blob(body)

        self.assertEqual(extract_text(blob), body.decode(), "this module must be right")

        ref = reference_body(blob)
        self.assertNotEqual(
            ref,
            body.decode(),
            "the reference's three-byte 0x82 read produced the SAME answer as the "
            "canonical four-byte read, so there was no conflict to resolve",
        )
        self.assertTrue(
            ref.startswith("\x00"),
            f"expected the reference to glue a stray NUL on the front; got "
            f"{masked(ref, 8)!r}",
        )

        # And the widths the two implementations AGREE on must still agree, or
        # the disagreement above would just be noise.
        for length in (50, 5000):
            small = make_blob(b"q" * length)
            self.assertEqual(extract_text(small), reference_body(small))

        # 0x87 is the reference's other gap: it returns None where we read it.
        wide = make_blob(b"a forced 0x87", tag=0x87)
        self.assertIsNone(reference_body(wide))
        self.assertEqual(extract_text(wide), "a forced 0x87")

        # And on 0x83 the two AGREE — both refuse it. The reference gets the
        # right answer for the wrong reason (it refuses everything >= 0x80 it
        # does not know); this module refuses it because 0x83 is the
        # floating-point tag. Worth pinning: the brief's table would have had
        # us read eight bytes here, which is the one change that would have
        # made this module WORSE than the thing it replaces.
        floaty = (
            REAL_PREFIX + b"NSString" + REAL_FILLER + MARKER
            + b"\x83" + b"\x0c\x00\x00\x00\x00\x00\x00\x00" + b"swallowed!!!"
        )
        self.assertIsNone(reference_body(floaty))
        self.assertIsNone(extract_text(floaty))


# ---------------------------------------------------------------------------
# 5. THE GROUND-TRUTH GATE — the real database.
# ---------------------------------------------------------------------------


class TestGroundTruthAgainstTheRealDatabase(unittest.TestCase):
    """Decode every row Apple wrote BOTH ways, and demand we agree with Apple.

    The bar is 99.9% exact with ZERO exceptions.  It is not 100% because a
    database written by a decade of OS versions is allowed one weird row; it is
    not 99% because a one-in-a-hundred failure over tens of thousands of rows is
    hundreds of silently wrong entries on people's cards.

    The percentage gate needs enough rows to mean something.  A Mac whose history
    holds fewer than :attr:`MIN_OVERLAP` rows carrying both columns (a new Mac, or
    one whose texts all arrived after Apple stopped filling ``text``) skips the
    percentage with the count in the reason; the zero-exceptions check still runs
    on whatever rows there are.
    """

    GATE = 99.9

    #: Fewer overlap rows than this cannot prove a decoder correct to 99.9%.
    MIN_OVERLAP = 500

    @classmethod
    def setUpClass(cls):
        cls.con, cls.reason = open_chat_db()
        if cls.con is None:
            return
        cls.exact = 0
        cls.mismatched = 0
        cls.raised = 0
        cls.examples = []
        for text, blob in cls.con.execute(
            "select text, attributedBody from message "
            "where text is not null and attributedBody is not null"
        ):
            try:
                decoded = extract_text(blob)
            except Exception as exc:  # noqa: BLE001 — the contract says never
                cls.raised += 1
                if len(cls.examples) < 5:
                    cls.examples.append(f"RAISED {exc.__class__.__name__}: {exc}")
                continue
            if decoded == text:
                cls.exact += 1
            else:
                cls.mismatched += 1
                if len(cls.examples) < 5:
                    cls.examples.append(
                        f"text[{len(text)}]={masked(text)!r} vs "
                        f"decoded[{len(decoded) if decoded is not None else 0}]="
                        f"{masked(decoded)!r}"
                    )
        cls.total = cls.exact + cls.mismatched + cls.raised

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "con", None) is not None:
            cls.con.close()

    def setUp(self):
        if self.con is None:
            self.skipTest(self.reason)

    def _require_enough_overlap(self):
        """Skip, with the count, where this Mac's history is too thin to gate on."""
        if self.total < self.MIN_OVERLAP:
            self.skipTest(
                f"only {self.total:,} rows on this Mac carry both columns, fewer than "
                f"the {self.MIN_OVERLAP:,} a 99.9% gate needs to mean something. The "
                "synthetic tests above still prove the decoder; this gate runs on a "
                "Mac with more history."
            )

    def test_the_overlap_set_is_big_enough_to_mean_something(self):
        print(f"\n[ground truth] overlap rows (text AND attributedBody): {self.total:,}")
        self._require_enough_overlap()
        self.assertGreaterEqual(self.total, self.MIN_OVERLAP)

    def test_zero_exceptions_were_raised(self):
        self.assertEqual(
            self.raised,
            0,
            f"extract_text RAISED on {self.raised:,} of {self.total:,} real rows. "
            "The never-raises contract is what stops one bad row killing a whole "
            f"morning's intake. Examples: {self.examples}",
        )

    def test_decoding_matches_apples_own_text_column(self):
        self._require_enough_overlap()
        pct = (self.exact / self.total * 100) if self.total else 0.0
        print(
            f"[ground truth] exact {self.exact:,}/{self.total:,} = {pct:.4f}%  "
            f"(mismatched {self.mismatched:,}, raised {self.raised:,})"
        )
        self.assertGreaterEqual(
            pct,
            self.GATE,
            f"the decoder agrees with Apple's own `text` column on only "
            f"{pct:.4f}% of {self.total:,} real rows, below the {self.GATE}% gate. "
            f"Masked examples (digits redacted, truncated): {self.examples}",
        )

    def test_clean_never_turns_a_successful_decode_into_none(self):
        """clean() must only ever narrow a string, never signal failure."""
        rows = 0
        checked = 0
        for (blob,) in self.con.execute(
            "select attributedBody from message where attributedBody is not null "
            "limit 5000"
        ):
            rows += 1
            decoded = extract_text(blob)
            if decoded is None:
                continue
            checked += 1
            self.assertIsNotNone(
                clean(decoded),
                "clean() returned None for a body that decoded successfully, "
                "which would report a real message as unreadable",
            )
        if rows == 0:
            self.skipTest("no row on this Mac carries an attributedBody yet")
        self.assertGreater(
            checked, 0, f"none of {rows:,} attributedBody rows decoded, so clean() was never tried"
        )
        print(f"[ground truth] clean() held its contract on {checked:,} decoded bodies")


# ---------------------------------------------------------------------------
# 6. The claims this build rests on, measured rather than assumed.
# ---------------------------------------------------------------------------


class TestTheClaimsBehindThisDecoder(unittest.TestCase):
    """Why the decoder is the PRIMARY read path, measured on this machine.

    Two claims: ``text`` is dead, and ``attributedBody`` decodes.  The second is
    the one asserted hard, because it stays the right assertion even if Apple
    ever repopulates ``text``.  The first is measured and PRINTED loudly, so a
    machine where it no longer holds says so instead of quietly drifting.  Both
    are about the shape of THIS Mac's history, so a Mac with no rows of the kind
    measured skips with the reason rather than failing.
    """

    @classmethod
    def setUpClass(cls):
        cls.con, cls.reason = open_chat_db()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "con", None) is not None:
            cls.con.close()

    def setUp(self):
        if self.con is None:
            self.skipTest(self.reason)

    def test_text_is_null_on_essentially_every_recent_row(self):
        total, with_text, with_blob, blob_only = self.con.execute(
            "select count(*), "
            "sum(case when text is not null then 1 else 0 end), "
            "sum(case when attributedBody is not null then 1 else 0 end), "
            "sum(case when text is null and attributedBody is not null then 1 else 0 end) "
            "from message"
        ).fetchone()
        if not total:
            self.skipTest("the Messages database on this Mac holds no message yet")
        print(
            f"\n[claims] all time: {total:,} rows · text NOT NULL {with_text:,} "
            f"({with_text / total * 100:.2f}%) · attributedBody NOT NULL {with_blob:,} "
            f"({with_blob / total * 100:.2f}%) · blob-only {blob_only:,}"
        )
        months = self.con.execute(
            "select strftime('%Y-%m', datetime(date/1000000000 + 978307200, 'unixepoch')), "
            "count(*), sum(case when text is not null then 1 else 0 end) "
            "from message group by 1 order by 1"
        ).fetchall()
        for ym, n, tn in months:
            print(f"[claims]   {ym}  rows={n:>6,}  text NOT NULL={tn:>6,} ({tn / n * 100:5.2f}%)")

        if not blob_only:
            # A store-shape fact about THIS Mac, not a defect: every row that has
            # a blob also has a text column, so the decoder is a second path here
            # rather than the only one. Said plainly instead of failing.
            self.skipTest(
                "every row with an attributedBody on this Mac also has a text column, "
                "so the decoder is a belt-and-braces path here rather than the only one"
            )
        self.assertGreater(blob_only, 0)

        recent = months[-6:]
        r_rows = sum(n for _, n, _ in recent)
        r_text = sum(t for _, _, t in recent)
        r_pct = r_text / r_rows * 100 if r_rows else 0.0
        if r_pct >= 50:
            print(
                "[claims] *** LOUD NOTE: `text` is populated on "
                f"{r_pct:.2f}% of the last six months' rows. The premise "
                "('NULL on essentially every recent row') does not hold "
                "on this machine. The decoder is still correct, but it is a "
                "belt-and-braces path rather than the only one. Report this. ***"
            )
        else:
            print(
                f"[claims] last six months: text NOT NULL on {r_text:,} of "
                f"{r_rows:,} rows ({r_pct:.2f}%) — the decoder is the ONLY read path"
            )

    def test_the_decoder_recovers_the_rows_text_lost(self):
        """The load-bearing claim: blob-only rows really do come back as text."""
        total = 0
        decoded = 0
        empty = 0
        for (blob,) in self.con.execute(
            "select attributedBody from message "
            "where text is null and attributedBody is not null"
        ):
            total += 1
            body = extract_text(blob)
            if body is None:
                continue
            decoded += 1
            if not clean(body):
                empty += 1
        if not total:
            self.skipTest(
                "no row on this Mac has lost its text column, so there is nothing to recover"
            )
        pct = decoded / total * 100 if total else 0.0
        print(
            f"[claims] blob-only rows: {total:,} · decoded {decoded:,} ({pct:.3f}%) · "
            f"of those {empty:,} are attachment/app payloads with no words"
        )
        self.assertGreaterEqual(
            pct,
            99.0,
            f"only {pct:.3f}% of the {total:,} rows that have NO text column could "
            "be decoded, so this plug-in would be blind on the rows it exists to "
            "read.",
        )

    def test_the_length_marker_census_on_real_data(self):
        """The evidence behind the 0x82 resolution, re-measured every run.

        The census is independent of :mod:`imtypedstream` on purpose: it walks
        the bytes itself, so it cannot inherit the decoder's idea of the format.
        """
        counts = {
            "single 0x00-0x7f": 0,
            "0x81": 0,
            "0x82": 0,
            "0x87": 0,
            "0x83 (float tag!)": 0,
            "other >=0x80": 0,
        }
        longest = 0
        nul_led = 0
        for (blob,) in self.con.execute(
            "select attributedBody from message where attributedBody is not null"
        ):
            data = bytes(blob)
            i = data.find(b"NSString")
            if i < 0:
                continue
            j = data.find(b"+", i)
            if j < 0 or j + 1 >= len(data):
                continue
            tag = data[j + 1]
            if tag == 0x81:
                counts["0x81"] += 1
            elif tag == 0x82:
                counts["0x82"] += 1
                body = extract_text(data)
                if body is not None and body.startswith("\x00"):
                    nul_led += 1
            elif tag == 0x87:
                counts["0x87"] += 1
            elif tag == 0x83:
                counts["0x83 (float tag!)"] += 1
            elif tag >= 0x80:
                counts["other >=0x80"] += 1
            else:
                counts["single 0x00-0x7f"] += 1
            body = extract_text(data)
            if body is not None:
                longest = max(longest, len(body.encode("utf-8")))
        print(
            "\n[markers] " + " · ".join(f"{k}: {v:,}" for k, v in counts.items())
            + f" · longest body {longest:,} bytes"
        )
        if counts["0x82"] == 0 and counts["0x87"] == 0:
            print(
                "[markers] NOTE: no 0x82 or 0x87 row on this machine — the wide "
                "branches are exercised only by the synthetic tests above."
            )
        self.assertEqual(
            nul_led,
            0,
            f"{nul_led} of the {counts['0x82']} real 0x82 rows decoded with a "
            "leading NUL, which is the exact signature of a three-byte length "
            "read. The decoder has regressed to the reference's bug.",
        )
        self.assertEqual(
            counts["0x83 (float tag!)"] + counts["other >=0x80"],
            0,
            "a length marker that is NOT 0x81/0x82/0x87 turned up in the real "
            f"data ({counts['0x83 (float tag!)']:,} of them 0x83, the "
            f"floating-point tag; {counts['other >=0x80']:,} others). Those rows "
            "are being refused, so real messages are being dropped, and the "
            "format assumption in imtypedstream's docstring needs revisiting "
            "before this ships.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
