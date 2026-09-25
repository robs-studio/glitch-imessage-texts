"""iMessage intake — the redaction pass, the last gate before text reaches disk.

Everything above this module reads the member's Messages database; this module is
what stands between what it read and a file on disk.  Give it a message body, get
back the same body with secret-*shaped* spans replaced by the literal
:data:`REDACTION`.  It is pure: no database, no filesystem, no network, no
configuration, no model, no pip dependency, standard library only.  Same input,
same output, on every machine and every run — the redaction of a body must not
depend on a clock, a config file, or what ran before it.

READ THIS FIRST — what this module does NOT protect
=====================================================
This is a **shape filter, not a confidentiality guarantee.**  It finds things
that *look like* machine-issued secrets.  It cannot find things that are secret
because of who said them.  Stated plainly, because the member is entitled to
know exactly what they are getting:

* **It does not make the stored thread private.**  The thread file still holds
  the whole conversation in plain text on this machine.  Redaction removes a
  handful of secret-shaped tokens from it; it does not encrypt it, does not
  lock it, and does not reduce who on this Mac can read it.
* **It does not catch a secret said in ordinary words.**  "The spare key is
  under the mat on the left."  "Don't tell anyone, but we're separating."
  "His biopsy came back positive."  A spouse's door code typed as "the usual
  one" or "same as the garage".  None of these have a shape, so none of them
  are touched.  A confidence shared in plain words, a health detail, a term of
  a business negotiation: this module reads all of them and leaves every word
  in place.
* **It does not catch a password made of words.**  "the password is sunshine"
  is left alone, because the value carries no digit and no quotes and the rule
  that would catch it would also eat "the password is on the fridge" and
  "the password is in my email" — see :data:`_SECRET_VALUE`.  Only a
  digit-bearing or quoted value is taken.
* **It does not catch an address, a date of birth, a licence number, a social
  security number written with dashes, a medical record number, or a name.**
  None of those are in scope here.
* **It is deliberately biased towards leaving text alone.**  Every threshold
  below was chosen so that ordinary prose survives untouched, because a
  redactor that eats "$1,200 for the repair" and "Psalm 121:7-8"
  makes the stored thread unreadable and the member stops trusting the feature.
  A rule that would catch more secrets at the cost of eating prose was not
  added.  **That trade-off means misses are expected and by design.**
* **It is not a review.**  Nothing here flags a body as sensitive, asks anybody
  about it, or declines to store it.  The only decision it makes is "these
  exact characters look like a machine-issued secret, so replace them".

The project's rule is "no secrets in the vault, ever".  This module moves the
needle on the machine-issued end of that — the one-time codes, the card digits,
the door PIN typed in a hurry — and **that is all it does**.  The rest of the
rule is carried by where the threads are stored and who can read them, not here.

The false-positive bar — why the rules are this narrow
========================================================
A member's texts are family, work, community and everything between.  Numbers
are everywhere in them and almost none of them are secrets: chapter-and-verse
references (``Psalm 121:7-8``, ``3 John 11``, ``Acts 18:8-10``), appointment times,
prices, phone numbers, attendance figures, visit counts.  A rule
loose enough to eat those destroys the feature outright, so every rule here is
anchored on **surrounding words**, not on digits alone.  A bare six-digit number
is never enough on its own; ``123456`` in isolation is left exactly as it is.

Each rule's docstring below names what it catches **and what it deliberately
does not**, and every item in the list above has a test in
``tests/test_imredact.py`` asserting it comes through untouched.

Measured against the live store
================================
The rules were not designed in the abstract.  They were run over real message
bodies from this Mac's ``~/Library/Messages/chat.db`` and tuned against what
came back.  The final figures are recorded in ``tests/test_imredact.py``, which
re-measures them on every run rather than trusting this docstring — a hit rate
that climbs is a rule that has gone loose, and the suite fails on it.

Overlap — the rule when two rules claim the same characters
=============================================================
Rules are matched independently and the winners are chosen **leftmost-longest**:
hits are sorted by start offset, then by descending length, then by the rule's
position in :data:`RULE_NAMES`, and a hit that overlaps one already accepted is
dropped.  The result is a set of non-overlapping spans in increasing offset
order, which is what :func:`find_hits` returns and exactly what :func:`redact`
replaces.  The tie-break is total, so the same body always yields the same
answer; there is no "it depends which rule ran first".

Idempotency — why ``[redacted]`` can never be re-eaten
========================================================
``redact(redact(x)) == redact(x)`` is a hard invariant, because a body can be
re-redacted by a re-run, a backfill, or a caller being careful twice.  Two things
hold it:

1. no rule can match :data:`REDACTION` on its own — every rule needs either
   digits or a digit-bearing value, and ``[redacted]`` has neither; and
2. :func:`find_hits` additionally drops any hit that overlaps an existing
   :data:`REDACTION` literal in the body, so even a future rule that *could*
   match it would still not be allowed to.

Point 2 is belt-and-braces on purpose: point 1 is a property of today's rules
and could be lost by a well-meant edit, while point 2 is structural.  Keep both.

Never raises
=============
Nothing in this module raises.  It sits in the middle of an unattended morning
run; a body that trips it must cost that body's redaction, not the run.  A
``None`` body returns ``None``, an empty body returns ``""``, and a body that is
somehow not a string at all is returned **unchanged** after one stderr line,
because silently replacing it with ``"[redacted]"`` would destroy data over what
is an upstream type bug.  The caller's contract is ``str | None``; that is what
:class:`imchat.Message` already guarantees.
"""

from __future__ import annotations

import re
import sys

# imconfig first: importing it is what puts this plug-in's folder ahead of the
# engine's on sys.path for the whole process. This module needs nothing from it,
# but every module in the plug-in imports it first so the ordering is never a
# question of which one happened to be imported.
import imconfig  # noqa: F401

# ---------------------------------------------------------------------------
# The replacement.
# ---------------------------------------------------------------------------

#: What every redacted span becomes. Lower-case, square-bracketed, and chosen so
#: it reads as an obvious hole in a sentence rather than as content. It carries
#: no digits and no quotes, which is why no rule below can match it — see the
#: module docstring's idempotency section.
REDACTION: str = "[redacted]"

#: Finds :data:`REDACTION` already present in a body. Any hit that overlaps one
#: of these is dropped, which is what makes re-redaction a no-op structurally
#: rather than by luck.
_ALREADY_REDACTED = re.compile(re.escape(REDACTION))

# ---------------------------------------------------------------------------
# Shared fragments. Built as strings and composed below rather than written out
# per rule, so a boundary that is right in one rule is right in all of them.
# ---------------------------------------------------------------------------

#: No letter or digit immediately before. This is what keeps a digit run inside
#: an alphanumeric token — ``1Z999AA10123456784`` — from being seen as a number
#: at all. A ``+`` is also excluded so an E.164 international phone number
#: (``+8613812345678``, thirteen digits) is never read as a long digit run.
_LEFT_EDGE: str = r"(?<![+0-9A-Za-z])"

#: No letter or digit immediately after. Same reason, other end.
_RIGHT_EDGE: str = r"(?![0-9A-Za-z])"

#: The gap a rule may step over between its anchor word and its digits: a short
#: run of characters that contains **no digits** (so it can never skip past one
#: number to reach another) and **no sentence break** (so "Your code. Call me at
#: 5551234" is two sentences, not one match). Non-greedy, so the nearest number
#: wins. ``{0,24}`` is about four words — enough for "your verification code for
#: Acme Bank is", short enough not to wander into the next clause.
_NEAR: str = r"[^0-9\n.!?;]{0,24}?"

#: The wider version, for the shapes where the anchor trails the digits
#: ("483920 is your Apple ID verification code").
_NEAR_WIDE: str = r"[^0-9\n.!?;]{0,32}?"

#: Words that turn a following "code" into something that is *not* a secret.
#: Written as a stack of individually fixed-width negative lookbehinds because
#: Python's :mod:`re` refuses a single variable-width one; stacking them is
#: legal and is the only way to say "not this word, immediately before".
#: ``re.IGNORECASE`` is set on every rule, so these match any casing.
#: Without this stack, "your zip code is 90210" and "error code: 1234" are both
#: read as one-time codes.
_NOT_A_SECRET_CODE: str = (
    r"(?<!zip )(?<!zip-)(?<!area )(?<!area-)(?<!dress )(?<!error )"
    r"(?<!promo )(?<!postal )(?<!country )(?<!source )(?<!barcode )"
    r"(?<!bar )(?<!qr )(?<!morse )(?<!coupon )(?<!referral )"
    r"(?<!discount )(?<!routing )(?<!tax )(?<!sort )(?<!swift )"
    r"(?<!status )(?<!color )(?<!colour )(?<!cpt )(?<!icd )(?<!ada )"
    r"(?<!procedure )(?<!diagnosis )(?<!billing )(?<!npi )(?<!exit )"
)

# ---------------------------------------------------------------------------
# Rule 1 — one-time / verification codes.
# ---------------------------------------------------------------------------

#: The words that mark a following "code" as machine-issued. A "code" with none
#: of these in front of it, and no "your" in front of it, is not a secret here.
_OTP_QUALIFIER: str = (
    r"(?:verification|verify|verifying|security|one[\s-]?time|single[\s-]?use|"
    r"login|log[\s-]?in|sign[\s-]?in|signin|access|confirmation|confirm|"
    r"authentication|authorisation|authorization|auth|activation|activate|"
    r"2fa|mfa|otp|temporary|temp|secret|unlock|recovery|reset|"
    r"registration|enrollment|enrolment)"
)

#: The nouns a one-time code is called by.
_OTP_NOUN: str = r"(?:code|passcode|pass\s?code|pin|password|token)"

#: The digits themselves. Four is the shortest code any real service issues;
#: ten is past the longest (six is near-universal) and stops the run from
#: growing into something that is plainly not a code. Hard edges on both sides,
#: so ``121:7-8`` and ``09/22`` can never contribute a "code".
_OTP_DIGITS: str = _LEFT_EDGE + r"(?P<secret>\d{4,10})" + _RIGHT_EDGE

#: The verbs that make "<digits> to <verb>" a code rather than an amount.
#: "Send 1500 to confirm the booking" is money; "Enter 483920 to confirm" is a
#: code. The difference is the leading verb, so the leading verb is required.
_OTP_USE_VERB: str = r"(?:use|using|enter|reply\s+with|text\s+back|text)"
_OTP_PURPOSE: str = (
    r"(?:verify|confirm|activate|authenticate|log\s?in|sign\s?in|continue|"
    r"complete\s+(?:your\s+)?(?:sign[\s-]?in|log[\s-]?in|registration|"
    r"verification|setup))"
)

_OTP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # (a) "<qualifier> code ... <digits>" — "Your verification code is 483920",
    #     "one-time passcode: 4821", "2FA code 129384". The qualifier must sit
    #     immediately before the noun, which is what keeps "zip code" out.
    re.compile(
        _OTP_QUALIFIER + r"[\s-]+" + _NOT_A_SECRET_CODE + _OTP_NOUN + r"\b"
        + _NEAR + _OTP_DIGITS,
        re.IGNORECASE,
    ),
    # (b) "your [up to two words] code ... <digits>" — "Your code is 483920",
    #     "Your Apple ID code is 483920". The lookbehind stack is what stops
    #     "your zip code is 90210" and "your area code is 202".
    re.compile(
        r"\byour\s+(?:[A-Za-z][A-Za-z0-9'’]{0,14}\s+){0,2}"
        + _NOT_A_SECRET_CODE + _OTP_NOUN + r"\b" + _NEAR + _OTP_DIGITS,
        re.IGNORECASE,
    ),
    # (c) "<digits> is your ... code" — the shape most services actually send,
    #     "483920 is your Apple ID verification code". The digits lead, so the
    #     anchor is checked behind them.
    re.compile(
        _OTP_DIGITS + r"\s+is\s+(?:your|the|my)\b" + _NEAR_WIDE
        + _NOT_A_SECRET_CODE + _OTP_NOUN + r"\b",
        re.IGNORECASE,
    ),
    # (d) Google's "G-483920". A letter, a hyphen, and the code; unmistakable,
    #     and the one bare-prefix shape worth special-casing because Google
    #     sends it to almost everyone.
    re.compile(
        r"\bG-(?P<secret>\d{4,8})" + _RIGHT_EDGE,
        re.IGNORECASE,
    ),
    # (e) "Enter 483920 to verify" / "Reply with 4821 to confirm". Both ends are
    #     anchored: a use-verb in front and a purpose-verb behind, so an amount
    #     of money in the same sentence shape is not touched.
    re.compile(
        _OTP_USE_VERB + r"\s+(?:the\s+)?(?:code\s+)?" + _OTP_DIGITS
        + _NEAR + r"\bto\s+" + _OTP_PURPOSE + r"\b",
        re.IGNORECASE,
    ),
)

# ---------------------------------------------------------------------------
# Rule 2 — long digit runs.
# ---------------------------------------------------------------------------

#: How many unbroken digits it takes to be a long run.
#:
#: **Thirteen, and the number is argued rather than picked.** The rule has to
#: clear every phone number a member writes and still reach the shortest
#: thing worth catching:
#:
#: * a US phone typed bare — ``2125550143`` — is **10** digits;
#: * the same number with a country code — ``12025550123`` — is **11**;
#: * a twelve-digit run is the longest thing that is still plausibly a phone
#:   number typed without punctuation (a country code plus a long national
#:   number), so the threshold has to be past it;
#: * **13 is the shortest payment card that exists** (the legacy 13-digit Visa),
#:   with Diners at 14, Amex at 15 and Visa/Mastercard/Discover at 16. Anything
#:   above 13 would stop covering cards at all at the short end.
#:
#: So 13 is the *only* value that clears every phone shape and still reaches the
#: shortest card. It leaves exactly one known gap — a 12-digit Maestro card,
#: which is not issued in the United States — and that gap is preferred to
#: eating a phone number, because a phone number in a thread is the whole point
#: of the feature.
LONG_RUN_MIN_DIGITS: int = 13

#: And how many is too many to still be one.
#:
#: **Nineteen — the ISO/IEC 7812 ceiling on a payment card number.** A US bank
#: account number tops out at 17 and a policy number rarely passes 19, so
#: nothing a member would want hidden runs longer than this. A run of twenty or
#: more consecutive digits is a *machine identifier*, and the first measurement
#: of this module against a live store proved exactly that: without the cap
#: the rule produced **dozens of spans per matched body**, and every one
#: inspected was a URL query-string id (a Facebook ``fbid``, a Google Drive
#: ``ouid``, an X status id, a chain of UTM ad ids), the longest a single span
#: hundreds of digits long. Redacting those does not hide a secret; it shreds a
#: link the member shared and can no longer open.
#:
#: The cap is written as ``{13,19}`` against a hard right edge, so a 21-digit
#: run is **not matched at all** rather than having nineteen digits bitten out
#: of the front of it: the length alternatives all fail the right edge, and
#: every later start position fails the left edge. "Too long to be a card"
#: therefore means "left alone", which is the only sane reading.
LONG_RUN_MAX_DIGITS: int = 19

#: URL punctuation a card number is never glued to, and a query-string id always
#: is. Stacked onto the left edge of the run rules, this is the other half of the
#: URL fix: every long run inspected in that first measurement sat immediately
#: behind ``=``, ``/`` or ``.``. A card in a text message is written after a
#: space, a newline or nothing at all — never after an ``&``.
#:
#: The set is RFC 3986's unreserved marks and sub-delimiters, and ``-`` is in it
#: for a measured reason: with the first five characters guarded, the only
#: URL-shaped false positives left over a whole real store were LinkedIn share
#: links, whose 19-digit ``…-activity-<id>-aBcD?…`` slug sits behind a hyphen.
#: Redacting that id breaks a link the member shared. Nothing is lost by the
#: guard, because a card typed into a message is never glued to a hyphen either
#: — written with hyphens it is grouped in fours, which the pattern below reads
#: as a whole.
_NOT_URL_GLUED: str = r"(?<![=/&?#._~-])"

_LONG_RUN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        _LEFT_EDGE + _NOT_URL_GLUED
        + r"(?P<secret>\d{"
        + str(LONG_RUN_MIN_DIGITS) + r"," + str(LONG_RUN_MAX_DIGITS)
        + r"})" + _RIGHT_EDGE
    ),
    # The same digits written the way a card is actually printed: groups of
    # four, one separator throughout, starting on a real card prefix.
    # ``4111 1111 1111 1111`` and ``4111-1111-1111-1111`` are cards.
    #
    # Three constraints keep this off prose, and the first is the important one:
    #
    # * the run must open with **3, 4, 5 or 6** — the ISO/IEC 7812 major
    #   industry identifiers that every major payment card
    #   starts with (3 Amex/Diners, 4 Visa, 5 Mastercard, 6 Discover). This is
    #   what stops ``2020 2021 2022``, a perfectly ordinary list of years, from
    #   reading as a card: it opens with a 2;
    # * the separator is captured once and back-referenced, so a mixed
    #   separator is not a card (``9-20-2026 9-21-2026`` is two dates);
    # * groups of exactly four, which is the card convention and not the phone
    #   one (``202 555 0123`` is 3-3-4 and never matches).
    #
    # Deliberately NOT taken: the Amex 4-6-5 grouping (``3782 822463 10005``).
    # Written solid it is a 15-digit run and the rule above has it; written in
    # Amex's own grouping it is missed, and that miss is preferred to a looser
    # group-size rule that would start finding dates and years.
    re.compile(
        _LEFT_EDGE + _NOT_URL_GLUED
        + r"(?P<secret>[3-6]\d{3}(?P<sep>[ -])\d{4}(?P=sep)\d{4}"
        r"(?:(?P=sep)\d{1,4})?)" + _RIGHT_EDGE
    ),
)

# ---------------------------------------------------------------------------
# Rule 3 — "the password is ...", "PIN is ...", "the code for the door is ...".
# ---------------------------------------------------------------------------

#: Nouns that name a shared secret outright. ``number`` is **not** here and must
#: never be added: "my number is 2125550143" is a phone number the member wants
#: kept, and it is on the false-positive bar for exactly that reason.
_SECRET_NOUN: str = (
    r"(?:passwords?|passwd|pass\s?words?|passcodes?|pass\s?codes?|"
    r"passphrases?|pins?|pin\s?codes?|combos?|combinations?)"
)

#: Places whose "code" is a door PIN rather than a reference number.
_PLACE_NOUN: str = (
    r"(?:door|doors|gate|garage|lock|lockbox|key\s?box|alarm|safe|keypad|"
    r"entry|building|gym|pool|shed|storage|unit|locker|vault|"
    r"wi[\s-]?fi|wifi|network|router|hotspot|guest)"
)

#: The connector between the noun and the value. **Required**, never optional:
#: without it, "pin 2026 budget" and "combo 1234 works" become redactions. Every
#: shape on the brief carries one ("password is", "passcode is", "PIN is",
#: "the code for the door is"), so requiring it costs nothing real.
_ASSIGN: str = r"\s*(?:is|are|=|:)\s*"

#: The value a secret-phrase rule will take, and the tightest part of this
#: module.
#:
#: Two forms only:
#:
#: * a **quoted** string — quoting a value in a sentence like this is itself the
#:   signal; and
#: * an unquoted token that **contains at least one digit**.
#:
#: The digit requirement is what makes the rule usable at all. Without it,
#: "the password is on the fridge" redacts to "the password is [redacted] the
#: fridge" and "the password is in my email" loses "in" — the rule would be
#: eating English. With it, ``4821``, ``hunter2`` and ``bluehouse22`` are taken
#: and every wordy value is left alone.
#:
#: **The cost is stated plainly in the module docstring: an all-letters password
#: is NOT caught.** That is a real miss, accepted deliberately, because the rule
#: that would catch it cannot tell a password from a preposition.
#:
#: The charset excludes ``[`` and ``]``, so :data:`REDACTION` can never be a
#: value even before the overlap guard runs.
_SECRET_VALUE: str = (
    r"(?P<secret>"
    r'"[^"\n]{2,40}"'
    r"|'[^'\n]{2,40}'"
    r"|(?=[A-Za-z0-9._!@#$%^&*+/-]{0,39}\d)"
    r"[A-Za-z0-9._!@#$%^&*+/-]{2,39}[A-Za-z0-9]"
    r")" + _RIGHT_EDGE
)

_SECRET_PHRASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # (a) "the password is hunter2", "PIN: 4821", "passcode = 9931".
    re.compile(
        r"\b" + _SECRET_NOUN + r"\b" + _ASSIGN + _SECRET_VALUE,
        re.IGNORECASE,
    ),
    # (b) "the code for the door is 4821", "combination to the shed is 1122".
    #     The "for"/"to" is what makes a bare "code" a secret here.
    #
    #     ``key`` is deliberately NOT one of these nouns. "the key to a good
    #     wedding toast is 1000 words" is ordinary English and would be
    #     redacted; "code for" and "combination to" have no such idiom.
    re.compile(
        r"\b(?:code|combo|combination)\s+(?:for|to)\s+"
        r"(?:the\s+|our\s+|my\s+|your\s+)?(?:[A-Za-z][A-Za-z0-9'’-]{0,14}\s+){0,3}?"
        + _ASSIGN + _SECRET_VALUE,
        re.IGNORECASE,
    ),
    # (c) "door code is 4821", "wifi password is bluehouse22", "gate pin: 1234".
    re.compile(
        r"\b" + _PLACE_NOUN + r"\s+(?:code|password|passcode|pin|combo|"
        r"combination)\b" + _ASSIGN + _SECRET_VALUE,
        re.IGNORECASE,
    ),
)

# ---------------------------------------------------------------------------
# Rule 4 — card tails.
# ---------------------------------------------------------------------------

#: The last four digits, **excluding anything year-shaped**.
#:
#: ``(?!19\d\d|20\d\d)`` is the whole design decision. "ending in 4821" is a card
#: tail; "the lease ends in 2026" and "my term ends in 2027" are dates, and a
#: member writes far more of the second kind than the first. Blocking
#: 1900-2099 costs 200 of the 10,000 possible tails (2%) and buys back every
#: sentence about a year, which is the right side of that trade.
_TAIL_DIGITS: str = (
    _LEFT_EDGE + r"(?!19\d\d|20\d\d)(?P<secret>\d{4})" + _RIGHT_EDGE
)

#: The same four digits with **no** year exclusion, for the masked-prefix shape.
#: ``••••2026`` is unambiguous: nothing writes a year behind four bullets, so the
#: exclusion that protects prose is not needed and would only cause a miss.
_TAIL_DIGITS_MASKED: str = _LEFT_EDGE + r"(?P<secret>\d{4})" + _RIGHT_EDGE

_CARD_TAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # (a) "ending in 4821", "ends with 4821", "card ending in 4821".
    re.compile(
        r"\bend(?:s|ed|ing)?\s+(?:in|with)\s*[#:]?\s*" + _TAIL_DIGITS,
        re.IGNORECASE,
    ),
    # (b) "last four 4821", "last 4 digits are 4821", "last four digits: 4821".
    re.compile(
        r"\blast\s+(?:four|4)\s*(?:digits?)?\s*(?:are|is|:|=)?\s*"
        + _TAIL_DIGITS,
        re.IGNORECASE,
    ),
    # (c) "••••4821", "xxxx 4821", "****-4821". Three or more mask characters,
    #     so a stray "**" cannot start it.
    re.compile(
        r"(?:[*x•·●•·#]{3,})\s*-?\s*" + _TAIL_DIGITS_MASKED,
        re.IGNORECASE,
    ),
)

# ---------------------------------------------------------------------------
# The rule table.
# ---------------------------------------------------------------------------

#: ``(rule name, compiled pattern)``, in the order a tie between two equal spans
#: is broken. A rule may own more than one pattern — the shapes of a one-time
#: code do not fit one readable regex, and forcing them to would trade clarity
#: for nothing. Every pattern MUST define a group named ``secret``: that group,
#: not the whole match, is what gets replaced, so "your verification code is
#: 483920" becomes "your verification code is [redacted]" and stays a readable
#: sentence.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    [("otp_code", p) for p in _OTP_PATTERNS]
    + [("long_digit_run", p) for p in _LONG_RUN_PATTERNS]
    + [("password_or_pin", p) for p in _SECRET_PHRASE_PATTERNS]
    + [("card_tail", p) for p in _CARD_TAIL_PATTERNS]
)

#: Every rule name :func:`redact` can produce, in tie-break order. The CP3
#: invariant sweep and the test suite import this, so it is the public list: a
#: rule added without its name landing here is a rule nothing can audit.
RULE_NAMES: tuple[str, ...] = (
    "otp_code",
    "long_digit_run",
    "password_or_pin",
    "card_tail",
)

#: ``rule name -> tie-break position``, built once from :data:`RULE_NAMES`.
_RULE_ORDER: dict[str, int] = {name: i for i, name in enumerate(RULE_NAMES)}

# Said at most once per process: a body that is not a string is an upstream type
# bug, and one line is enough to find it without drowning a morning run.
_warned_bad_type = False


def _warn(message: str) -> None:
    """One plain line to stderr. Degrading loudly beats wedging the tool."""
    print(f"[imessage] {message}", file=sys.stderr)


def find_hits(body: str) -> list[tuple[str, int, int]]:
    """``(rule_name, start, end)`` for every span :func:`redact` would replace.

    The spans are **non-overlapping and sorted by start offset**, and they are
    exactly what :func:`redact` substitutes — ``body[start:end]`` is the text
    that disappears. That equivalence is the point of this function: it is how a
    test proves the offsets, and how an audit can count what a rule is doing
    without ever printing the text it matched.

    Overlaps are resolved leftmost-longest, then by the rule's position in
    :data:`RULE_NAMES` (see the module docstring). Any candidate that overlaps a
    :data:`REDACTION` literal already present in ``body`` is dropped, which is
    what makes re-redaction a structural no-op.

    Never raises. A body that is not a string returns ``[]``.

    Args:
        body: The message text to scan.

    Returns:
        A list of ``(rule_name, start, end)``, ascending by ``start``.
    """
    if not isinstance(body, str) or not body:
        return []

    protected = [m.span() for m in _ALREADY_REDACTED.finditer(body)]

    candidates: list[tuple[int, int, int, str]] = []
    for name, pattern in _RULES:
        try:
            matches = list(pattern.finditer(body))
        except re.error:  # pragma: no cover - a compiled pattern does not fail
            continue
        for match in matches:
            start, end = match.span("secret")
            if start < 0 or end <= start:
                continue
            if any(start < p_end and p_start < end for p_start, p_end in protected):
                continue
            # Sort key: start ascending, length descending, rule order ascending.
            candidates.append((start, -(end - start), _RULE_ORDER[name], name))

    if not candidates:
        return []

    candidates.sort()

    hits: list[tuple[str, int, int]] = []
    last_end = -1
    for start, neg_length, _order, name in candidates:
        if start < last_end:
            continue
        end = start - neg_length
        hits.append((name, start, end))
        last_end = end
    return hits


def redact(body: str | None) -> str | None:
    """Replace secret-shaped spans with the literal ``[redacted]``.

    ``None`` in, ``None`` out — a body the decoder could not read stays
    unreadable rather than becoming an empty string, because :class:`imchat`
    keeps "could not be read" (``None``) and "carries no words" (``""``) apart
    and this module must not collapse them.

    ``redact(redact(x)) == redact(x)`` always holds; see the module docstring.

    Never raises. A body that is somehow not a string is returned **unchanged**
    after one stderr line for the process: replacing it wholesale would destroy
    data over what is an upstream type bug, and the caller's contract is
    ``str | None``.

    Args:
        body: The message text, or ``None``.

    Returns:
        The redacted text, or ``None`` when ``body`` was ``None``.
    """
    global _warned_bad_type

    if body is None:
        return None
    if not isinstance(body, str):
        if not _warned_bad_type:
            _warned_bad_type = True
            _warn(
                f"a message body arrived as {type(body).__name__}, not text; it is "
                "passed through un-redacted. Bodies must reach redact() as str."
            )
        return body
    if not body:
        return body

    hits = find_hits(body)
    if not hits:
        return body

    pieces: list[str] = []
    cursor = 0
    for _name, start, end in hits:
        pieces.append(body[cursor:start])
        pieces.append(REDACTION)
        cursor = end
    pieces.append(body[cursor:])
    return "".join(pieces)
