"""iMessage intake: the morning stage's entry script.  KEEP THIS FILE TINY AND STABLE.

Why tiny: the engine's consent key is sha256(capability.json + THIS FILE's bytes)
(morning_reports.approval_hash).  Every byte here is part of the member's yes, so any
edit, even a comment, pauses the feed until they approve again.  All the logic lives in
imbrief.py, which is not hashed and can change freely.

What it guarantees, whatever state the helpers are in: exactly one JSON object,
{"headline": str, "items": [str, ...]}, on stdout, and exit 0.  Three failures in a
row (a non-zero exit, output that is not that object) pause the feed, so a broken
helper (an ImportError, a SyntaxError, a stray print) must never reach the engine.
imbrief's output is captured and checked; anything else printed goes to stderr; and
if no valid object came out, the fixed FALLBACK below is printed instead.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

FALLBACK = json.dumps({
    "headline": "Texts: the morning brief could not run properly, so there is no texts "
                "report this morning.",
    "items": ["Run `imessage.py status` to see where your texts stand."],
})


def _one_object(text: str) -> str | None:
    """The last line printed, re-serialised as ASCII, if it is the one valid object."""
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        data = json.loads(lines[-1])
        items = data["items"]
        if isinstance(data["headline"], str) and isinstance(items, list) and all(
            isinstance(i, str) for i in items
        ):
            line = json.dumps({"headline": data["headline"], "items": items})
            return line if len(line) < 60_000 else None
    except Exception:  # noqa: BLE001 - anything unexpected means "not the object"
        pass
    return None


def run() -> int:
    captured = io.StringIO()
    try:
        folder = str(Path(__file__).resolve().parent)
        if folder not in sys.path:
            sys.path.insert(0, folder)
        with contextlib.redirect_stdout(captured):
            import imbrief

            imbrief.main()
    except BaseException as exc:  # noqa: BLE001 - one JSON object and exit 0, always
        print(f"[imessage] the morning brief failed ({type(exc).__name__}: {exc})",
              file=sys.stderr)
    line = _one_object(captured.getvalue())
    stray = captured.getvalue().strip()
    if stray and (line is None or len(stray.splitlines()) > 1):
        print(f"[imessage] stray output kept off stdout: {stray[:2000]}", file=sys.stderr)
    with contextlib.suppress(OSError):
        sys.stdout.write((line or FALLBACK) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
