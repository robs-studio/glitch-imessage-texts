"""iMessage intake — turning ``message.attributedBody`` into readable text.

This module is the plug-in's PRIMARY read path for what a message actually said,
and everything downstream is worthless if it decodes wrong.  It is pure: no
database, no filesystem, no network, no configuration, standard library only.
Give it bytes, get back a string.  It imports cleanly on Windows and Linux even
though the data it parses only exists on a Mac, because nothing in it is
platform-specific — the caller owns the platform question, not the decoder.

Why the decoder is the primary path and not a fallback
-------------------------------------------------------
``message.text`` used to hold the message body, and a great deal of sample code
on the internet still reads only that column.  On a current Mac that code would
now return nothing.  Measured against one member's live
``~/Library/Messages/chat.db``, ``text`` stopped being populated around January
2026 (the share of each month's rows that still carry a ``text`` value)::

    2025-12   text NOT NULL  98%
    2026-01   text NOT NULL  41%     <- the cutover
    2026-02   text NOT NULL   0.2%
    2026-04   text NOT NULL   0.02%
    2026-08   text NOT NULL   0.1%

Across the whole database under a third of rows carry a ``text`` value, against
more than 99% with an ``attributedBody``: roughly two rows in three that ONLY the
decoder can read.  (Every share in this docstring is a snapshot of one store.
The shape is what matters, and the test suite re-measures it on whatever Mac
runs it rather than trusting these numbers.)  Reading
``text`` alone is therefore
not a degraded read, it is a silent one: the rows come back, the bodies are
empty, nothing raises, and a year of conversation quietly disappears.  So this
module is what the plug-in calls, and ``text`` is only ever a cross-check.

The format: NSArchiver "typedstream", not a property list
----------------------------------------------------------
``attributedBody`` holds the ``NSAttributedString`` that Messages renders,
serialised by the old ``NSArchiver`` in Apple's **typedstream** format.  It is
NOT ``NSKeyedArchiver``, so :mod:`plistlib` cannot read it — it will raise on the
header and no amount of coaxing changes that.  There is no typedstream reader in
the standard library and this plug-in takes no pip dependency, so the bytes are
walked by hand.

Format reference (the reverse-engineering this module is built on):
https://chrissardegna.com/blog/reverse-engineering-apples-typedstream-format/

A blob opens with the fixed header ``b"\\x04\\x0bstreamtyped"``, then an object
graph.  The message body is the first ``NSString`` in that graph.  The walk is
deliberately shallow — find the class name ``NSString``, step to the ``+``
(0x2B) type marker that introduces an inline string, read the length, take that
many bytes — because a full object-graph parser is a great deal more code and
more ways to be wrong for a payload whose shape has been stable for years.  The
shallow walk is held honest by the ground-truth gate in
``tests/test_imtypedstream.py``, which decodes every row that still has BOTH
``text`` and ``attributedBody`` and demands an exact match.

The length prefix — and the 0x82 bug this module does NOT inherit
------------------------------------------------------------------
A typedstream integer is variable width.  A single byte carries a small value;
a tag byte escapes to a wider little-endian integer:

===========  ===============================================================
tag          what follows
===========  ===============================================================
``0x81``     2 bytes, little-endian — VERIFIED on thousands of real rows
``0x82``     4 bytes, little-endian — VERIFIED byte-for-byte on a real row
``0x87``     8 bytes, little-endian — unreachable here; see below
``0x83``     NOT a length: the floating-point tag.  Refused.
``0x84``     NOT a length: "new object/string".  Refused.
``0x85``     NOT a length: nil.  Refused.
``0x86``     NOT a length: end of object.  Refused.
anything     the byte IS the value (a signed 8-bit int); ``0x80``–``0xFF``
             therefore reads negative and is refused
===========  ===============================================================

The reference measurement script this plug-in was built from reads **three**
bytes after ``0x82`` and advances three, and does not handle ``0x83`` at all.
That three is wrong, and it is not a harmless wrong: it is a live bug that fired
on real data.  The evidence, taken from one real store:

* A **single** row in the whole store used ``0x82``: a message body a little
  over 32,767 bytes, the int16 ceiling (see below).
* Worked through with a body of 40,000 bytes: the bytes at the marker are
  ``82 40 9c 00 00``.  ``40 9c 00 00`` little-endian is 40,000.  Reading only
  ``40 9c 00`` gives 40,000 as well, because the top byte is zero, and it was
  zero on the real row too — so the **length** survives the mistake.
* The **cursor** does not.  Advancing three instead of four leaves the reader one
  byte early, so the reference decodes that body as ``"\\x00"`` + all but its
  last byte: a stray NUL glued to the front and the last byte of the message lost.
* That row's ``text`` column is NULL, so the row is not in the
  ``text``/``attributedBody`` overlap the reference was measured against.  Its
  "100.0% exact" is true and still missed this, which is exactly why the
  measurement set and the production set have to be reasoned about separately.

The same measurement settles two other open questions:

* ``0x81`` is unambiguously 2 bytes (thousands of real rows use it, and every
  value fits comfortably inside two bytes).
* The 2-byte integer is **signed**.  The real ``0x82`` body was over 32,767 and
  under 65,535 bytes, so it would fit an *unsigned* int16, and Apple's encoder
  escalated it to ``0x82`` anyway, which
  it would only do if its int16 ceiling were 32,767.  So this module reads every
  escaped width signed, matching the format rather than guessing.

For values ``0x00``–``0x7F`` the signed and unsigned readings are identical, so
the third disagreement in the brief (signed vs unsigned single byte) is a
non-issue in practice; it is written signed here to match the format, and the
census found no single-byte marker in ``0x80``–``0xFF`` at all, so the negative
branch has never been taken on real data.

Why 0x83 is REFUSED and 0x87 is the 8-byte tag
------------------------------------------------
A widely repeated table — including in the brief this module was built from, and
in several public bug reports against other iMessage readers — says the escapes
are powers of two: ``0x81``→2, ``0x82``→4, ``0x83``→8.  The first two are right.
**The third is folklore**, and two independent sources say so:

* The blog cited above, by the author of ``imessage-exporter``/``crabstep``, in
  its "Further Discoveries" section: *"0x83 is not the 8-byte integer indicator;
  it introduces a floating-point value, whose width comes from the type tag"*
  and *"The 8-byte integer indicator is 0x87, outside the range I had
  predicted."*
* ``dgelessus/python-typedstream``, an unrelated Python implementation, whose
  constants read ``_TAG_INTEGER_2 = 0x81``, ``_TAG_INTEGER_4 = 0x82``,
  ``_TAG_FLOATING_POINT = 0x83``, ``_TAG_NEW = 0x84``, ``_TAG_NIL = 0x85``,
  ``_TAG_END_OF_OBJECT = 0x86``; its integer reader accepts ``0x81`` and
  ``0x82`` and raises on everything else.

Even the bug reports that publish the powers-of-two table label its wide rows
"corrected by inspection, not exercised by real data … a reviewer should treat
them as unverified".  They were inferring a pattern, not reading bytes.

This matters more than the unreachability suggests.  ``0x83``–``0x86`` are not
spare values: they are live control tags that appear throughout every real blob
(the fixed prefix before the first ``NSString`` is full of
``0x84``).  A decoder that treats ``0x83`` as "read the next 8 bytes as a
length" would, on any blob whose shape it did not expect, swallow eight bytes of
object graph as a length and return whatever followed.  Refusing is both correct
and safer, and it costs nothing: a ``0x83`` can never legitimately introduce a
string length, because a string length is an integer and a float is not one.

``0x87`` is implemented for fidelity to the primary source, and is unreachable
here by four orders of magnitude — the largest real blob measured was under
50 KB, and a body would have to exceed 2,147,483,647 bytes before a
4-byte length overflowed.  No row in the database uses ``0x83`` or ``0x87`` as a
length; the test suite re-measures that on every run and fails if it ever
changes.

Never raising is the contract, not a convenience
--------------------------------------------------
:func:`extract_text` wraps its whole body and returns ``None`` for anything it
cannot read.  This is not defensive padding.  The caller walks tens of thousands
of rows in a run with a hard time budget; one malformed blob — a half-written
row, a message type whose payload is not an ``NSString`` at all — must cost one
skipped message, not the whole morning's intake.  And unlike :mod:`imconfig`,
this module says **nothing** to stderr on a bad row: a per-row warning over tens
of thousands of rows is not a diagnostic, it is a denial of service on the log.  A caller that
wants to know how many rows failed counts its own ``None`` returns.

``None`` versus the empty string — a real distinction, kept
-------------------------------------------------------------
The two "no words" answers mean opposite things and this module keeps them apart:

``None``
    **Failure.**  The bytes could not be read as a message body.  The caller
    learned nothing about what was said.

``""`` (from :func:`clean`)
    **Success.**  The body was read and it genuinely contains no words — an
    attachment on its own, a sticker, a Digital Touch, an app payload.  About
    2.5% of real ``attributedBody`` rows are this case.  It is
    ordinary, it is not an error, and treating it as one would drop every
    photo-only message.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# The format's fixed bytes. See the module docstring before changing any of them.
# ---------------------------------------------------------------------------

#: The fixed opening of every typedstream archive. A blob that does not start
#: with this is not a typedstream, and guessing past it is how a decoder starts
#: returning plausible-looking rubbish instead of saying no.
HEADER: bytes = b"\x04\x0bstreamtyped"

#: The class name of the object that carries the message body. The FIRST one in
#: the graph is the text; later ones belong to attribute runs.
NSSTRING: bytes = b"NSString"

#: The typedstream type marker for an inline string, ``+`` (0x2B). The length
#: prefix begins at the very next byte.
STRING_MARKER: bytes = b"+"

#: Tag byte -> how many little-endian bytes of length follow it. A marker byte
#: that is not a key here IS the length, read as a signed 8-bit integer — which
#: makes every OTHER byte in 0x80-0xFF read negative, and a negative length is
#: refused. That is exactly the wanted behaviour for 0x83 (floating point),
#: 0x84 (new), 0x85 (nil) and 0x86 (end of object): they are live control tags,
#: not lengths, and must never be consumed as one. Read the module docstring's
#: "Why 0x83 is REFUSED" section before adding a key here.
_WIDE_LENGTH_TAGS: dict[int, int] = {
    0x81: 2,
    0x82: 4,
    0x87: 8,
}

#: U+FFFC OBJECT REPLACEMENT CHARACTER. Messages puts one of these in the body
#: wherever an attachment, sticker or inline app payload sits. It is a placeholder
#: for a thing that is not text, so it is never part of what was *said*.
OBJECT_REPLACEMENT: str = "￼"


# ---------------------------------------------------------------------------
# The decoder.
# ---------------------------------------------------------------------------


def extract_text(blob: bytes | None) -> str | None:
    """The message body inside a typedstream ``attributedBody``, or ``None``.

    Walks the blob shallowly: confirm the :data:`HEADER`, find the first
    :data:`NSSTRING`, step to the :data:`STRING_MARKER` that follows it, read the
    variable-width length, and decode that many bytes as UTF-8 with
    ``errors="replace"``.  ``replace`` rather than ``strict`` because a mojibake
    character in a message is still a message worth keeping, while an exception
    here would cost the caller the row.

    **This function never raises.**  Every failure — ``None`` or empty input, a
    blob that is not a typedstream, no ``NSString`` in the graph, no marker after
    it, a length prefix cut short, a negative length, a length that runs off the
    end of the buffer — returns ``None``.  See the module docstring for why that
    is the contract and not merely politeness, and for why nothing is logged.

    A length that overruns the buffer is refused rather than truncated.  Python
    would happily hand back a short slice and the caller could not tell the
    difference between a whole message and the front of one, which is the worst
    of the available answers: silently wrong beats loudly wrong in no system that
    files things onto a person's card.  Measured on a real store, every blob
    decoded cleanly and **none** overran, so this strictness costs nothing real.

    Accepts ``bytes``, ``bytearray`` or ``memoryview`` — sqlite3 hands back
    ``bytes`` for a BLOB column, but a caller that has sliced or buffered the row
    should not have to think about it.

    Args:
        blob: The raw ``message.attributedBody`` bytes, or ``None``.

    Returns:
        The decoded body as a string, which may legitimately be empty if the
        archived ``NSString`` was empty; or ``None`` if the bytes could not be
        read as a message body at all.
    """
    try:
        if not blob:
            # None, b"" and an empty buffer all mean "no body here". Checked
            # before the coercion below so a None never reaches bytes().
            return None

        data = bytes(blob)

        if not data.startswith(HEADER):
            return None

        name_at = data.find(NSSTRING)
        if name_at < 0:
            return None

        marker_at = data.find(STRING_MARKER, name_at)
        if marker_at < 0:
            return None

        # The byte immediately after the marker is either the length itself or a
        # tag saying how many length bytes follow.
        cursor = marker_at + 1
        if cursor >= len(data):
            return None

        tag = data[cursor]
        cursor += 1

        width = _WIDE_LENGTH_TAGS.get(tag)
        if width is None:
            # The tag IS the value, as a signed 8-bit integer. Signed and
            # unsigned agree for 0x00-0x7F, which is every single-byte length
            # ever seen on real data. Everything else in 0x80-0xFF — notably
            # the control tags 0x83 float, 0x84 new, 0x85 nil, 0x86 end-of-object
            # — reads negative here and is refused by the `length < 0` check
            # below, which is the correct answer: none of them introduces a
            # string length.
            length = tag - 256 if tag >= 0x80 else tag
        else:
            if cursor + width > len(data):
                # The length prefix itself is cut short — a truncated blob.
                return None
            length = int.from_bytes(
                data[cursor : cursor + width], "little", signed=True
            )
            cursor += width

        if length < 0:
            return None
        if cursor + length > len(data):
            return None

        return data[cursor : cursor + length].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — the never-raises contract is the point.
        # Deliberately broad, and deliberately silent. One unreadable row must
        # cost one row, never the run, and never a line of log per row.
        return None


def clean(text: str | None) -> str | None:
    """Strip the attachment placeholders, keeping ``None`` and ``""`` distinct.

    Removes every :data:`OBJECT_REPLACEMENT` (U+FFFC) from the string and then
    trims surrounding whitespace.  Messages plants one of those characters
    wherever a non-text object sits in the body: on its own for a photo, a
    sticker or a Digital Touch, and in the middle of a sentence when someone
    sends a picture with a caption.  It is a placeholder for a thing that is not
    text, so it is never part of what was said, and a caller measuring "how much
    was actually written here" must not count it.

    The trailing whitespace trim is load-bearing rather than tidiness: an
    attachment-with-a-trailing-space body of ``"\\ufffc "`` would otherwise clean
    to ``" "``, which is not empty, so the caller would treat a photo as a
    message with content.  Trimming is what makes "empty means attachment" true.

    The two empty-ish answers mean opposite things and are both returned
    deliberately:

    * ``None`` in gives ``None`` out — :func:`extract_text` could not read the
      bytes, so there is nothing to clean and nothing is known about the message.
      **This is a failure.**
    * ``""`` out means the body WAS read and carries no words: an attachment, a
      sticker or an app payload. **This is not a failure**, it is about 2.5% of
      real rows, and a caller that treats it as one drops every
      photo-only message. A caller filtering for substance should skip it; a
      caller counting messages should still count it.

    Never raises.

    Args:
        text: A body from :func:`extract_text`, or ``None``.

    Returns:
        ``None`` if ``text`` was ``None``; otherwise the body with every U+FFFC
        removed and the ends trimmed, possibly the empty string.
    """
    if text is None:
        return None
    try:
        return text.replace(OBJECT_REPLACEMENT, "").strip()
    except Exception:  # noqa: BLE001 — same never-raises contract as above.
        return None
