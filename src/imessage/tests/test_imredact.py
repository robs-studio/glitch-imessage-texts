"""The redaction suite — what gets hidden, and far more importantly what does not.

``imredact`` is the last thing between a member's messages and a file on disk,
and it fails in two opposite directions.  Too loose and it eats ordinary prose:
chapter-and-verse references, appointment times, prices, phone numbers — the stored
thread becomes unreadable and the member stops trusting the feature.  Too tight
and a one-time code, a door PIN or a card number lands in the vault.  This suite
holds both walls up at once.

The shape of it
----------------
* :class:`TestFalsePositiveBar` is the wall on the loose side: every phrase this
  member really writes, asserted to come through **byte-for-byte untouched**.
  :class:`TestFalsePositiveBarBites` is its negative control — it proves those
  phrases really are a minefield, by showing a rule written without anchors
  would damage most of them.  Without that control the bar could be quietly
  defused by editing the digits out of it and nothing would go red.
* Each rule then gets its positives, and a **minimal-pair control**: a phrase
  from the bar that must survive, beside a phrase differing by one word that
  must be caught (``"your zip code is 90210"`` survives, ``"your access code is
  90210"`` does not — same digits, different anchor).  That pairing is what
  stops the classic bad fix: silencing a false positive by breaking the rule.
  Gut the rule and the twin goes red in the same run.
* :class:`TestLiveCorpus` runs the real thing over the member's real store.

PRIVACY — the rule this file is written under
----------------------------------------------
The live tests read the member's actual messages.  **Nothing in this file may
print a real message body, or any part of one** — not into the test output, not
into a failure message.  Counts, rates and rule names only; :func:`_mask` exists
for anything else and replaces every digit and letter before it is shown.

Every fixture string below is invented, and none of it came out of the member's
store.  Every phone number is on the reserved ``555-01xx`` fictional block, in
whichever spelling the rule under test needs.  The card numbers are the published
test numbers that no issuer has ever issued (``4111 1111 1111 1111``,
``378282246310005``, ``5555555555554444``, ``6011111111111117``), the domains are
``example.com`` (RFC 2606), and the codes, PINs and passwords are made up.

The measured figures pinned here
---------------------------------
Taken over one real store's readable bodies when the rules were built:

===================  ==========
rule                 % of bodies
===================  ==========
``otp_code``            0.0858%
``long_digit_run``      0.0495%
``password_or_pin``     0.0231%
``card_tail``           0.0248%
**any rule**            0.1832%
===================  ==========

Every hit was inspected, digit-masked.  In prose the false-positive count was
**zero**: all but one of the long-run spans sat inside all-digit payload blobs
that contain no letters at all, and the last was a retailer's 15-digit order
number, which is an account number and squarely in that rule's stated scope.
The ceilings asserted by :class:`TestLiveCorpus` are set against these rates and
justified where they are defined; they are rates, so they hold on any member's
store, and :class:`TestLiveCorpus` re-measures every count it relies on.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The sorter is turned off across this block deliberately. Alphabetical order
# would put `imchat` above `imconfig`, and importing imconfig FIRST is what puts
# this plug-in's folder ahead of the engine's on sys.path — an ordering contract,
# not a style preference. Left to a formatter it would be "tidied" into a silent
# bug, so it is fenced here in the open rather than relying on nobody running
# `ruff --fix`.
# isort: off
import imconfig  # noqa: E402,F401

# Then the plug-in's own modules, before anything reachable only because
# imconfig put `.claude/scripts` on sys.path. imchat is this plug-in's module
# too; no engine module is imported anywhere in this suite.
import imchat  # noqa: E402
import imredact  # noqa: E402

import contextlib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import unittest  # noqa: E402

# isort: on


def _mask(text):
    """A shape, never a value. Digits to ``#``, letters to ``a``.

    Used for anything derived from a real body that reaches the output. Nothing
    in this suite prints a real body even masked, but a failure message that
    wants to show a shape has to go through here.
    """
    return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "#", str(text)))


# ---------------------------------------------------------------------------
# The false-positive bar — every one of these must survive byte-for-byte.
#
# These are not hypotheticals. Chapter-and-verse references, appointment times,
# prices, meeting numbers and phone numbers are what a member's texts are MADE
# of. A rule that eats any line below has destroyed the feature, however many
# secrets it catches.
# ---------------------------------------------------------------------------

FALSE_POSITIVE_BAR = (
    # Times and durations.
    ("see you at 4pm", "a time of day"),
    ("running 10 minutes late", "a duration"),
    ("I'll be there in 20", "a bare duration with no unit"),
    ("appointment on 09/22 at 2:30", "a date and a time"),
    # Phone numbers. A phone number is not a secret here — it is the whole
    # point of the feature, which keys interactions by handle.
    ("call me at 202 555 0123", "a spaced phone number"),
    ("my number is 2125550143", "a bare 10-digit phone number"),
    ("+1 202 555 0148", "an E.164-ish phone number"),
    ("202-555-0123", "a hyphenated phone number"),
    # Money and counts.
    ("$1,200 for the repair", "a price with a thousands comma"),
    ("$450", "a bare price"),
    ("visit 1040 of 1000", "two counts in one line"),
    ("we had 1000 visitors at the fair", "a headline count"),
    ("2026", "a bare year"),
    # Chapter-and-verse references. Common in study-group and book-club texts,
    # and shaped like nothing else.
    ("see section 121:7-8 of the handbook", "a section reference with a range"),
    ("Book 3 chapter 11", "a book-and-chapter reference"),
    ("Chapter 18:8-10", "a chapter-and-verse range"),
    ("Ch. 4:2, Sec. 32:21", "two references in one line"),
    # Reference numbers that are not secrets.
    ("tracking 1Z999AA10123456784", "a UPS tracking number"),
    ("order 114-1234567-7654321", "an Amazon order number"),
    ("flight DL1423 at 6:05", "a flight number"),
    ("confirmation ABC123XY", "an alphanumeric confirmation"),
    # The words that look like anchors but are not.
    ("your zip code is 90210", "a zip code"),
    ("her area code is 4154", "an area code"),
    ("error code: 1234", "a software error code"),
    ("promo code 4821 at checkout", "a promo code"),
    # Years behind the words the card-tail rule keys on.
    ("the lease ends in 2026", "a lease ending in a year"),
    ("the contract ends in 2028", "a contract ending in a year"),
    ("passport expires 2027", "an expiry year"),
    ("school year ending in 2026", "a school year"),
    # Values that are words, not secrets.
    ("the password is on the fridge", "a password's LOCATION, not its value"),
    ("the password is in my email", "the same, other phrasing"),
    ("the key to a good talk is 1000 words", "the 'key to X is Y' idiom"),
    # Lists of numbers that are not cards.
    ("2020 2021 2022 2023", "four consecutive years"),
    ("9-20-2026 9-21-2026", "two dates, mixed grouping"),
    # Links. A redacted URL is a link the member can no longer open.
    (
        "https://example.com/photo.php?fbid=10158234567890123&set=a.1015",
        "a URL with a long query-string id",
    ),
    (
        "https://example.com/posts/name-activity-7123456789012345678-aBcD?x=1",
        "a URL with a hyphen-glued 19-digit id",
    ),
)


class TestFalsePositiveBar(unittest.TestCase):
    """Ordinary prose comes through untouched. This is the wall on the loose side."""

    def test_every_bar_entry_survives_byte_for_byte(self):
        for text, why in FALSE_POSITIVE_BAR:
            with self.subTest(why=why):
                self.assertEqual(
                    imredact.redact(text),
                    text,
                    f"redact() damaged {why}. A rule has gone loose: this is a "
                    "phrase the member really writes, and eating it makes the "
                    "stored thread unreadable.",
                )

    def test_every_bar_entry_produces_no_hits_at_all(self):
        # Stronger than the above: not merely "the output matches" but "no rule
        # even claimed a span". A rule that matched a zero-width span would pass
        # the equality check and still be a bug.
        for text, why in FALSE_POSITIVE_BAR:
            with self.subTest(why=why):
                self.assertEqual(
                    imredact.find_hits(text),
                    [],
                    f"a rule claimed a span in {why}.",
                )


#: A redactor written the obvious, naive way — anchored on digits and bare
#: keywords instead of on surrounding words. This is what ``imredact`` would be
#: if nobody had thought about the member's actual texts, and it exists only to
#: prove the bar above is a real minefield.
_NAIVE = re.compile(
    r"\d{4,}"
    r"|\b(?:code|pin|password|passcode)\b\W{0,4}\S+"
    r"|\bend(?:s|ing)\s+in\s+\d+",
    re.IGNORECASE,
)

#: How many bar entries the naive redactor damages. Measured, then pinned: if a
#: future edit waters the bar down (deleting the digits, softening the phrases)
#: this number falls and the control goes red, which is the point.
NAIVE_DAMAGE_FLOOR = 25


class TestFalsePositiveBarBites(unittest.TestCase):
    """The negative control: prove the bar is dangerous, not decorative.

    A table of harmless-looking strings asserted to be unchanged is a test that
    passes for free if the strings are harmless.  These are not: a rule written
    without anchors mangles most of them, and this test measures that.  If
    someone "fixes" a failure by editing the bar instead of the rule, this goes
    red.
    """

    def test_a_naive_redactor_would_damage_most_of_the_bar(self):
        damaged = [why for text, why in FALSE_POSITIVE_BAR if _NAIVE.search(text)]
        self.assertGreaterEqual(
            len(damaged),
            NAIVE_DAMAGE_FLOOR,
            f"only {len(damaged)} of {len(FALSE_POSITIVE_BAR)} bar entries are "
            f"even at risk from a naive rule (floor {NAIVE_DAMAGE_FLOOR}). The "
            "bar has been watered down and no longer proves anything.",
        )

    def test_the_bar_assertion_itself_bites(self):
        # And prove the assertion in TestFalsePositiveBar is real by running it
        # against something that genuinely is a secret. If "assert unchanged"
        # could not fail, the whole class above would be worthless.
        with self.assertRaises(AssertionError):
            self.assertEqual(
                imredact.redact("your verification code is 483920"),
                "your verification code is 483920",
            )


# ---------------------------------------------------------------------------
# Per-rule positives, and the minimal pairs that stop a bad fix.
#
# Each MINIMAL_PAIRS entry is (survivor, twin, rule). The two strings differ by
# as little as one word, and often carry the SAME digits — so the only thing
# separating them is the anchor the rule is built on. Break the rule to silence
# the survivor and the twin goes red in the same run.
# ---------------------------------------------------------------------------

OTP_POSITIVES = (
    "Your verification code is 483920",
    "your code is 483920",
    "Your Apple ID code is 483920",
    "Your Apple Account Code is: 483920. Don't share it with anyone.",
    "483920 is your Apple ID verification code",
    "G-483920 is your Google verification code",
    "one-time passcode: 4821",
    "2FA code 129384",
    "your temporary sign-in code is 483920",
    "Enter 483920 to verify your account",
    "Reply with 4821 to confirm",
    "483920 is your code to log in to your account",
    "The temporary PIN you requested is 483920",
    "this is your portal security code: 483920",
)

LONG_RUN_POSITIVES = (
    "4111111111111111",
    "378282246310005",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "my card is 4111111111111111.",
    "6011111111111117",
    "5555555555554444",
)

PASSWORD_POSITIVES = (
    "the password is hunter2",
    "passcode is 4821",
    "PIN is 1234",
    "the code for the door is 4821",
    "door code is 4821",
    "garage code is 8841",
    "the wifi password is bluehouse22",
    'the password is "sunshine99"',
    "Passcode: 903214",
    "combination to the shed is 1122",
    "Laptop Pin:\n4821",
    "Login using the default password: wedding2026",
)

CARD_TAIL_POSITIVES = (
    "ending in 4821",
    "ends with 4821",
    "the card ending in 4821",
    "last four 4821",
    "last 4 digits are 4821",
    "last four digits: 4821",
    "your Debit card ending in 4821",
    "••••4821",
    "****-4821",
    "xxxx 4821",
)

MINIMAL_PAIRS = (
    # The anchor word is the ONLY difference. Same digits on both sides.
    ("your zip code is 90210", "your access code is 90210", "otp_code"),
    ("her area code is 4154", "her security code is 4154", "otp_code"),
    ("error code: 1234", "verification code: 1234", "otp_code"),
    # The threshold is the only difference: 11 digits is a phone, 13 is a card.
    ("my number is 12025550123", "my number is 1202555012345", "long_digit_run"),
    # Grouping and the leading digit are the only differences.
    ("2020 2021 2022 2023", "4020 2021 2022 2023", "long_digit_run"),
    # A password's location survives; a password's value does not.
    (
        "the password is on the fridge",
        "the password is on3thefridge",
        "password_or_pin",
    ),
    ("my number is 2125550143", "my pin is 2125550143", "password_or_pin"),
    # The year exclusion is the only thing protecting the survivor.
    ("the lease ends in 2026", "the lease ends in 4821", "card_tail"),
    ("school year ending in 2026", "school year ending in 4821", "card_tail"),
)


class _RuleCase(unittest.TestCase):
    """Shared assertions, in one place so every rule is held to the same bar."""

    def assert_caught(self, text, rule):
        hits = imredact.find_hits(text)
        names = {name for name, _s, _e in hits}
        self.assertIn(
            rule,
            names,
            f"{rule} did not fire. Fixture {text!r} is a secret this rule "
            f"exists to catch; rules that did fire: {sorted(names) or 'none'}.",
        )
        self.assertIn(
            imredact.REDACTION,
            imredact.redact(text),
            "find_hits claimed a span but redact() produced no redaction.",
        )


class TestOtpCode(_RuleCase):
    """One-time and verification codes."""

    def test_positives(self):
        for text in OTP_POSITIVES:
            with self.subTest(text=text):
                self.assert_caught(text, "otp_code")

    def test_a_bare_six_digit_number_is_never_enough(self):
        # The heart of the rule. Digits alone must not be a code, or every
        # chapter-and-verse reference and meeting statistic in the vault gets eaten.
        for text in ("483920", "code", "it's 483920", "483920 people came"):
            with self.subTest(text=text):
                self.assertEqual(imredact.redact(text), text)

    def test_only_the_digits_are_replaced_not_the_sentence(self):
        # Readability is the product. The sentence must survive the redaction.
        self.assertEqual(
            imredact.redact("Your verification code is 483920. Don't share it."),
            "Your verification code is [redacted]. Don't share it.",
        )

    def test_the_gap_does_not_cross_a_sentence_boundary(self):
        # "Your code. Call me at 5551234" is two sentences, not one code.
        text = "I forgot your code. Call me at 5551234"
        self.assertEqual(imredact.redact(text), text)


class TestLongDigitRun(_RuleCase):
    """Runs long enough to be a card, an account number or a policy number."""

    def test_positives(self):
        for text in LONG_RUN_POSITIVES:
            with self.subTest(text=text):
                self.assert_caught(text, "long_digit_run")

    def test_the_threshold_is_exactly_where_it_is_documented(self):
        # One digit below the threshold survives; the threshold itself is caught.
        # This is the assertion that pins LONG_RUN_MIN_DIGITS to a number rather
        # than a vibe, and it is why a 10- or 11-digit phone is safe.
        below = "9" * (imredact.LONG_RUN_MIN_DIGITS - 1)
        at = "9" * imredact.LONG_RUN_MIN_DIGITS
        self.assertEqual(imredact.redact(below), below, "the threshold slipped down")
        self.assertEqual(imredact.redact(at), imredact.REDACTION)

    def test_every_phone_length_this_member_writes_is_below_the_threshold(self):
        for digits in (10, 11, 12):
            with self.subTest(digits=digits):
                phone = "8" * digits
                self.assertLess(digits, imredact.LONG_RUN_MIN_DIGITS)
                self.assertEqual(imredact.redact(phone), phone)

    def test_a_run_too_long_to_be_a_card_is_left_whole(self):
        # Past the cap the rule must decline ENTIRELY, not bite the first 19
        # digits out of the middle of a machine id. Documented in the module.
        too_long = "9" * (imredact.LONG_RUN_MAX_DIGITS + 1)
        self.assertEqual(imredact.redact(too_long), too_long)
        self.assertEqual(imredact.find_hits(too_long), [])

    def test_digits_inside_an_alphanumeric_token_are_not_a_number(self):
        # The UPS tracking case, asserted on the mechanism rather than the
        # example: a digit run touching a letter is part of a token.
        text = "AA1234567890123456AA"
        self.assertEqual(imredact.redact(text), text)

    def test_a_url_id_is_not_a_card(self):
        # The defect the first live measurement found: without this guard the
        # rule produced dozens of spans per matched body, every one a
        # query-string id, and shredded links the member had shared.
        for url in (
            "https://example.com/p.php?fbid=10158234567890123&x=1",
            "https://example.com/a-activity-7123456789012345678-aBcD?s=1",
            "https://example.com/status/1234567890123456789",
            "https://example.com/x.html#1234567890123456",
        ):
            with self.subTest(url=url):
                self.assertEqual(imredact.redact(url), url)

    def test_an_international_number_written_with_a_plus_is_not_a_card(self):
        text = "+8613812345678"
        self.assertEqual(imredact.redact(text), text)


class TestPasswordOrPin(_RuleCase):
    """``password is …``, ``PIN is …``, ``the code for the door is …``."""

    def test_positives(self):
        for text in PASSWORD_POSITIVES:
            with self.subTest(text=text):
                self.assert_caught(text, "password_or_pin")

    def test_a_wordy_value_is_left_alone(self):
        # The documented miss, asserted so it stays a decision rather than
        # drifting into a rule that eats prepositions.
        for text in (
            "the password is on the fridge",
            "the password is in my email",
            "the password is the same as before",
        ):
            with self.subTest(text=text):
                self.assertEqual(imredact.redact(text), text)

    def test_a_connector_is_required(self):
        # Without this, "pin 2026 budget" and "combo 1234 works" are redactions.
        for text in ("pin 2026 budget", "combo 1234 works"):
            with self.subTest(text=text):
                self.assertEqual(imredact.redact(text), text)

    def test_number_is_not_a_secret_noun(self):
        # Load-bearing: a phone number is the feature, not a secret. If "number"
        # is ever added to the noun list, this goes red.
        text = "my number is 2125550143"
        self.assertEqual(imredact.redact(text), text)

    def test_the_sentence_survives(self):
        self.assertEqual(
            imredact.redact("the code for the door is 4821, see you soon"),
            "the code for the door is [redacted], see you soon",
        )


class TestCardTail(_RuleCase):
    """``ending in 4821``, ``last four 4821``."""

    def test_positives(self):
        for text in CARD_TAIL_POSITIVES:
            with self.subTest(text=text):
                self.assert_caught(text, "card_tail")

    def test_a_year_shaped_tail_is_deliberately_not_caught(self):
        # The trade documented in the module: 2% of possible tails given up to
        # keep every sentence about a year. When a lease, a contract or a school
        # year ends is an ordinary subject in anyone's texts.
        for year in ("1999", "2026", "2027", "2099"):
            with self.subTest(year=year):
                text = f"the lease ends in {year}"
                self.assertEqual(imredact.redact(text), text)

    def test_a_masked_prefix_overrides_the_year_exclusion(self):
        # Nothing writes a year behind four bullets, so the exclusion that
        # protects prose is not needed there and would only cause a miss.
        self.assert_caught("••••2026", "card_tail")

    def test_a_duration_is_not_a_tail(self):
        for text in ("ends in 10 minutes", "ending in 3 weeks", "ends in 20"):
            with self.subTest(text=text):
                self.assertEqual(imredact.redact(text), text)


class TestMinimalPairs(_RuleCase):
    """The control that stops a false positive being 'fixed' by gutting a rule.

    Each pair differs by about one word.  The survivor must survive and the twin
    must be caught, in the same run, by the named rule.  A rule loosened to catch
    the twin fails on the survivor; a rule tightened to spare the survivor fails
    on the twin.  There is no edit that satisfies one and not the other except
    the correct one.
    """

    def test_survivor_survives_and_twin_is_caught(self):
        for survivor, twin, rule in MINIMAL_PAIRS:
            with self.subTest(rule=rule, survivor=survivor):
                self.assertEqual(
                    imredact.redact(survivor),
                    survivor,
                    f"{rule} has gone loose: it ate {survivor!r}.",
                )
                self.assert_caught(twin, rule)


# ---------------------------------------------------------------------------
# The contract: None, empty, whole-body secrets, overlaps, idempotency, offsets.
# ---------------------------------------------------------------------------


class TestContract(unittest.TestCase):
    """The API surface, exactly as the caller is entitled to rely on it."""

    def test_none_in_none_out(self):
        # Not "", which imchat uses to mean "read, and genuinely wordless".
        # Collapsing the two would turn an unreadable body into a silent one.
        self.assertIsNone(imredact.redact(None))

    def test_empty_in_empty_out(self):
        self.assertEqual(imredact.redact(""), "")
        self.assertEqual(imredact.find_hits(""), [])

    def test_a_body_that_is_entirely_one_secret(self):
        self.assertEqual(imredact.redact("4111111111111111"), imredact.REDACTION)
        self.assertEqual(
            imredact.redact("Your verification code is 483920"),
            "Your verification code is [redacted]",
        )

    def test_two_secrets_in_one_body(self):
        text = "Your verification code is 483920 and the door code is 4821"
        out = imredact.redact(text)
        self.assertEqual(out.count(imredact.REDACTION), 2)
        self.assertNotIn("483920", out)
        self.assertNotIn("4821", out)
        self.assertIn("and the door code is", out)

    def test_a_non_string_body_passes_through_with_one_warning(self):
        # Never raises, and never replaces the body wholesale: destroying data
        # over an upstream type bug would be the worse failure.
        imredact._warned_bad_type = False
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            self.assertEqual(imredact.redact(b"bytes"), b"bytes")
        self.assertIn("not text", buffer.getvalue())
        self.assertEqual(imredact.find_hits(b"bytes"), [])

    def test_rule_names_is_complete_and_unique(self):
        self.assertEqual(len(imredact.RULE_NAMES), len(set(imredact.RULE_NAMES)))
        in_table = {name for name, _pattern in imredact._RULES}
        self.assertEqual(
            in_table,
            set(imredact.RULE_NAMES),
            "a rule exists that RULE_NAMES does not name, so nothing can audit "
            "it — or RULE_NAMES names a rule that no longer exists.",
        )

    def test_every_pattern_defines_the_secret_group(self):
        # The group, not the whole match, is what gets replaced. A pattern
        # missing it would redact its own anchor words and destroy the sentence.
        for name, pattern in imredact._RULES:
            with self.subTest(rule=name, pattern=pattern.pattern[:40]):
                self.assertIn("secret", pattern.groupindex)


class TestOverlap(unittest.TestCase):
    """Two rules claiming the same characters. The tie-break is total."""

    def test_leftmost_longest_wins(self):
        # "last four" + a full card: card_tail wants the first group of four,
        # long_digit_run wants the whole card. The longer span must win, or the
        # last twelve digits of a card number are written to disk.
        text = "last four 4111 1111 1111 1111"
        hits = imredact.find_hits(text)
        self.assertEqual(len(hits), 1)
        name, start, end = hits[0]
        self.assertEqual(name, "long_digit_run")
        self.assertEqual(text[start:end], "4111 1111 1111 1111")
        self.assertEqual(imredact.redact(text), "last four [redacted]")

    def test_hits_never_overlap_and_are_sorted(self):
        samples = (
            OTP_POSITIVES
            + LONG_RUN_POSITIVES
            + PASSWORD_POSITIVES
            + CARD_TAIL_POSITIVES
            + ("last four 4111 1111 1111 1111",
               "code is 483920, pin is 4821, ending in 5150")
        )
        for text in samples:
            with self.subTest(text=text):
                hits = imredact.find_hits(text)
                last_end = -1
                for _name, start, end in hits:
                    self.assertLess(start, end)
                    self.assertGreaterEqual(
                        start, last_end, "spans overlap or are out of order"
                    )
                    last_end = end

    def test_the_tie_break_is_deterministic(self):
        # Same body, same answer, every time and in any order of evaluation.
        text = "the password is 1234567890123456"
        first = imredact.find_hits(text)
        for _ in range(5):
            self.assertEqual(imredact.find_hits(text), first)


class TestIdempotency(unittest.TestCase):
    """``redact(redact(x)) == redact(x)``. A body can be redacted twice."""

    def test_redacting_twice_changes_nothing(self):
        samples = (
            OTP_POSITIVES
            + LONG_RUN_POSITIVES
            + PASSWORD_POSITIVES
            + CARD_TAIL_POSITIVES
            + tuple(text for text, _why in FALSE_POSITIVE_BAR)
        )
        for text in samples:
            with self.subTest(text=text):
                once = imredact.redact(text)
                self.assertEqual(imredact.redact(once), once)

    def test_the_redaction_literal_is_never_matched_by_any_rule(self):
        # Point 1 of the module's two-part guarantee: no rule can match it.
        for name, pattern in imredact._RULES:
            with self.subTest(rule=name):
                self.assertIsNone(pattern.search(imredact.REDACTION))

    def test_a_secret_beside_an_existing_redaction_is_still_caught(self):
        # Point 2 must not over-reach: the overlap guard protects the literal,
        # it does not switch the rest of the body off.
        text = "code is [redacted] and the door code is 4821"
        out = imredact.redact(text)
        self.assertEqual(out.count(imredact.REDACTION), 2)
        self.assertNotIn("4821", out)

    def test_a_body_made_only_of_redactions_is_untouched(self):
        text = "[redacted] [redacted] [redacted]"
        self.assertEqual(imredact.redact(text), text)
        self.assertEqual(imredact.find_hits(text), [])


class TestFindHitsOffsets(unittest.TestCase):
    """The offsets are exact: slicing the body yields what redact() replaced."""

    def test_slicing_by_each_span_reproduces_redact_exactly(self):
        samples = (
            OTP_POSITIVES
            + LONG_RUN_POSITIVES
            + PASSWORD_POSITIVES
            + CARD_TAIL_POSITIVES
            + ("last four 4111 1111 1111 1111",
               "code is [redacted] and the door code is 4821",
               "Your verification code is 483920 and the door code is 4821")
        )
        for text in samples:
            with self.subTest(text=text):
                hits = imredact.find_hits(text)
                # Rebuild what redact() must produce, using ONLY the offsets.
                pieces = []
                cursor = 0
                for _name, start, end in hits:
                    self.assertGreaterEqual(start, 0)
                    self.assertLessEqual(end, len(text))
                    pieces.append(text[cursor:start])
                    pieces.append(imredact.REDACTION)
                    cursor = end
                pieces.append(text[cursor:])
                self.assertEqual("".join(pieces), imredact.redact(text))

    def test_each_span_is_the_text_that_disappeared(self):
        text = "Your verification code is 483920"
        hits = imredact.find_hits(text)
        self.assertEqual(len(hits), 1)
        _name, start, end = hits[0]
        self.assertEqual(text[start:end], "483920")
        self.assertNotIn(text[start:end], imredact.redact(text))


# ---------------------------------------------------------------------------
# The live corpus. Real messages, counts only — never a body, never a fragment.
# ---------------------------------------------------------------------------

#: The ceiling on how many real bodies may match ANY rule.
#:
#: Measured 0.1832% on one real store. One percent is roughly a five-fold headroom
#: — room for the store to grow and for a new automated sender to start sending
#: codes — while still being one to two orders of magnitude below where a rule
#: that had started eating ordinary prose would land. A redactor firing on one
#: body in a hundred is not finding secrets; it is finding sentences.
LIVE_MAX_HIT_RATE = 1.0

#: The same ceiling per rule. Measured worst case 0.0858% (``otp_code``).
LIVE_MAX_RULE_HIT_RATE = 0.5

#: The ceiling on spans per hit body — and the one that actually caught the real
#: defect. When the long-run rule was eating URL query-string ids, the body-level
#: rate barely moved (0.2476% against 0.1832%) because the damage was
#: concentrated: **14.5 spans per matched body**, a single body redacted dozens
#: of times over. A body-rate ceiling alone would have passed that happily.
#: Measured after the fix: 1.07. Three is generous headroom and still less than a
#: quarter of what the broken version scored.
LIVE_MAX_SPANS_PER_HIT_BODY = 3.0

#: How many readable bodies a store must hold before a hit RATE means anything.
#: Below this, one automated sender can swing a percentage past its ceiling, so
#: the three rate ceilings skip (and say why) on a small store rather than fail
#: on a newcomer's Mac.  Whether the sweep really read the store is a separate
#: question, answered by re-measuring (see ``test_the_sweep_really_read_the_store``),
#: so a run that lost its access or its query still cannot report a flattering 0%.
LIVE_MIN_BODIES = 5000


def _check_hit_rate(case, hit_bodies, total_bodies, ceiling, what):
    """The hit-rate assertion, in ONE place so the control uses the real thing.

    Both :class:`TestLiveCorpus` and :class:`TestLiveCeilingBites` call this, so
    the control proves *this* assertion fails on a loose result rather than
    proving a look-alike written beside it.
    """
    rate = 100.0 * hit_bodies / total_bodies if total_bodies else 0.0
    case.assertLess(
        rate,
        ceiling,
        f"{what} matched {hit_bodies:,} of {total_bodies:,} real bodies "
        f"({rate:.4f}%), over the {ceiling}% ceiling. A high hit rate is a "
        "defect, not a feature: at this rate the rule is matching ordinary "
        "prose, and the stored threads are being shredded rather than cleaned.",
    )


@unittest.skipUnless(imchat.has_access()[0], "no access to the Messages store")
class TestLiveCorpus(unittest.TestCase):
    """The rules, over the member's real messages. Counts only, never content."""

    @classmethod
    def setUpClass(cls):
        cls.total = 0
        cls.hit_bodies = 0
        cls.spans = 0
        cls.per_rule_bodies = {name: 0 for name in imredact.RULE_NAMES}
        cls.per_rule_spans = {name: 0 for name in imredact.RULE_NAMES}
        cls.idempotent = 0
        cls.offsets_exact = 0
        cls.messages = 0

        conn = imchat.connect()
        try:
            # Re-measured on THIS store, never pinned: the module's own count of
            # the job, taken before and after the sweep because the store is live
            # (a text can arrive while this runs, which only ever pushes it up).
            cls.count_before = imchat.count_rows(conn)
            for message in imchat.iter_messages(conn):
                cls.messages += 1
                body = message.text
                if not body:
                    continue
                cls.total += 1
                hits = imredact.find_hits(body)
                if not hits:
                    continue
                cls.hit_bodies += 1
                cls.spans += len(hits)
                for name in {name for name, _s, _e in hits}:
                    cls.per_rule_bodies[name] += 1
                for name, _s, _e in hits:
                    cls.per_rule_spans[name] += 1

                once = imredact.redact(body)
                if imredact.redact(once) == once:
                    cls.idempotent += 1

                pieces = []
                cursor = 0
                for _name, start, end in hits:
                    pieces.append(body[cursor:start])
                    pieces.append(imredact.REDACTION)
                    cursor = end
                pieces.append(body[cursor:])
                if "".join(pieces) == once:
                    cls.offsets_exact += 1
            cls.count_after = imchat.count_rows(conn)
        finally:
            conn.close()

    def _require_a_measurable_store(self):
        """Skip a RATE ceiling on a store too small for a percentage to mean anything."""
        if self.total < LIVE_MIN_BODIES:
            self.skipTest(
                f"this Mac's Messages store has {self.total:,} readable bodies; a hit "
                f"rate needs at least {LIVE_MIN_BODIES:,} before its ceiling means "
                "anything, so it is not judged here."
            )

    def test_the_sweep_really_read_the_store(self):
        # A run that lost its access or its query reads a few rows, or none, and
        # would report a flattering 0%.  So the sweep is held to the store's own
        # size, re-measured here, rather than to any one member's history.
        if self.count_before == 0:
            # Readable but empty (a new Mac, or Messages never used here): that is
            # this machine's state, not a defect, and there is nothing to measure.
            self.skipTest("the Messages store on this Mac holds no messages yet")
        self.assertLessEqual(
            self.count_before,
            self.messages,
            f"the sweep read {self.messages:,} messages but the store counts "
            f"{self.count_before:,}; it lost its access or its query part-way.",
        )
        self.assertLessEqual(self.messages, self.count_after)
        self.assertGreater(
            self.total, 0,
            f"{self.messages:,} messages were read and not one carried readable "
            "words; the body decode has failed, so every rate below would be 0%.",
        )

    def test_overall_hit_rate_is_under_the_ceiling(self):
        self._require_a_measurable_store()
        _check_hit_rate(
            self, self.hit_bodies, self.total, LIVE_MAX_HIT_RATE, "the rules together"
        )
        # Reported so a drift is visible in the log before it is a failure.
        print(
            f"\n[redact] {self.total:,} real bodies · {self.hit_bodies:,} matched "
            f"({100.0 * self.hit_bodies / self.total:.4f}%) · {self.spans:,} spans"
        )

    def test_each_rule_is_under_its_own_ceiling(self):
        self._require_a_measurable_store()
        for name in imredact.RULE_NAMES:
            with self.subTest(rule=name):
                _check_hit_rate(
                    self,
                    self.per_rule_bodies[name],
                    self.total,
                    LIVE_MAX_RULE_HIT_RATE,
                    name,
                )
        print(
            "[redact] per rule: "
            + " · ".join(
                f"{name} {self.per_rule_spans[name]} spans/"
                f"{self.per_rule_bodies[name]} bodies"
                for name in imredact.RULE_NAMES
            )
        )

    def test_no_body_is_shredded(self):
        # The invariant that caught the URL defect. See the constant's comment.
        # A per-body ratio, so it needs no minimum store size.
        ratio = self.spans / self.hit_bodies if self.hit_bodies else 0.0
        self.assertLess(
            ratio,
            LIVE_MAX_SPANS_PER_HIT_BODY,
            f"{self.spans:,} spans across {self.hit_bodies:,} matched bodies "
            f"({ratio:.2f} per body, ceiling {LIVE_MAX_SPANS_PER_HIT_BODY}). A "
            "rule is firing many times inside single bodies, which is what "
            "eating URLs or structured payloads looks like. The body-level "
            "rate does not catch this; that is why this assertion exists.",
        )
        print(f"[redact] spans per matched body: {ratio:.2f}")

    def test_idempotent_on_every_real_body_that_matched(self):
        self.assertEqual(
            self.idempotent,
            self.hit_bodies,
            f"{self.hit_bodies - self.idempotent} real bodies changed on a "
            "second redaction. A re-run or a backfill would keep eating them.",
        )

    def test_offsets_are_exact_on_every_real_body_that_matched(self):
        self.assertEqual(
            self.offsets_exact,
            self.hit_bodies,
            f"{self.hit_bodies - self.offsets_exact} real bodies rebuilt from "
            "find_hits offsets did not match redact(). An audit counting those "
            "spans would be counting the wrong characters.",
        )


class TestLiveCeilingBites(unittest.TestCase):
    """The negative control for the live ceilings — they must be able to fail."""

    def test_the_hit_rate_assertion_fails_on_a_loose_result(self):
        with self.assertRaises(AssertionError):
            _check_hit_rate(self, 900, 10_000, LIVE_MAX_HIT_RATE, "a loose rule")

    def test_the_hit_rate_assertion_passes_on_the_measured_shape(self):
        # The measured rate (about 0.18%), at a round size.
        _check_hit_rate(self, 90, 50_000, LIVE_MAX_HIT_RATE, "the measured rate")

    def test_the_sweep_floor_skips_rather_than_fails_on_a_small_store(self):
        # The portability control: a newcomer's small store skips the rate
        # ceilings with a reason; it never turns them red.
        case = TestLiveCorpus("test_overall_hit_rate_is_under_the_ceiling")
        case.total = LIVE_MIN_BODIES - 1
        with self.assertRaises(unittest.SkipTest):
            case._require_a_measurable_store()
        case.total = LIVE_MIN_BODIES
        case._require_a_measurable_store()


if __name__ == "__main__":
    unittest.main()
