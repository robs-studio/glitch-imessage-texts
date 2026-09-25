"""A temp vault and a temp people database, so a test can stamp without touching the member.

``imspine`` is the plug-in's one road into the engine, and the engine's road into a
person card is real: ``people_stamp`` locks the card, backs it up into the memory
undo ring and replaces it; ``people_index`` projects it into ``memory.db``;
``people.py``'s accept door creates a person and projects it on its OWN connection.
Every one of those reads its location off the engine's ``config`` module at call
time.  So a fixture that moves only some of those locations does not isolate a
test, it splits it: the card lands in the temp folder and its backup lands in the
member's real undo ring.

Which locations move, and why each one
---------------------------------------
* ``config.MEMORY_DIR`` — ``people_index.project_changed`` resolves every card as
  ``MEMORY_DIR / rel``, and ``memory_snapshot.vault_name`` decides whether a file is
  a memory at all by whether it sits under it.
* ``config.MEMORY_SNAPSHOTS_DIR`` — **G3.**  ``memory_snapshot`` writes the pre-write
  backup under it.  Move the vault and forget this, and every stamp in a test drops a
  copy of a fixture card into the member's real undo ring, where ``memory_snapshot.py
  list`` would show it to them as a memory of theirs.
* ``config.PEOPLE_DIR`` — ``people_index`` enumerates declared ids from it,
  ``people.cmd_new`` writes the new card into it, and ``people._person_note_path``
  finds a card by scanning it.  It is computed from ``MEMORY_DIR`` once, at import,
  so moving ``MEMORY_DIR`` does NOT move it.
* every other ``config`` path that sits inside the real vault (``BRANDS_DIR``,
  ``MEETINGS_DIR``, ``DAILY_DIR``, …) — re-rooted wholesale by the same rule, so a
  code path this fixture did not foresee still cannot reach the real vault.
* ``config.DATABASE_PATH`` — ``imspine.open_conn`` is always handed the temp
  database, but the engine's accept and dismiss doors open their own connection
  with no path, which is ``DATABASE_PATH``.
* ``config.LOCAL_SNAPSHOTS_DIR`` — not on the stamp path; moved anyway because it is
  the other undo ring and costs nothing.

``start`` re-checks every one of those after patching and refuses to hand out a
fixture if any still points outside the temp folder.  ``stop`` puts every value
back exactly, and removes the temp folder.

The engine's own doors, used as the engine uses them
------------------------------------------------------
Proposals are raised through ``imspine.resolve`` (the real resolver) or
``people_db.insert_proposal``; they are dismissed and accepted through
``people.cmd_proposal_set`` and ``people.cmd_accept``, the functions the member's
``people.py proposals dismiss|accept`` command runs.  Those doors open their own
connection, and SQLite's writer lock is per database, so the fixture COMMITS its
own connection before calling one; otherwise the door would wait out
``busy_timeout`` (10 s) and fail.  ``dismiss(at=...)`` is the one deliberate
exception: it performs the same two writes the engine's door performs, on the
fixture's connection, with a chosen timestamp, because the day-precision rules
cannot be tested against a clock the test does not control.

Proving nothing escaped
-----------------------
:func:`real_state` lists the real ``people/`` folder and the real undo ring (names,
mtimes, sizes) and :func:`real_fixture_traces` looks for this fixture's own markers
in the real places, including a READ-ONLY look at the real ``memory.db``.  A test
takes the first before and after a full cycle, and asserts the second is empty.

Every name, number and address in here is invented: ``555-01xx`` is the reserved
fictional block and ``fixture.example.com`` sits under RFC 2606's ``example.com``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
if str(PLUGIN_HOME) not in sys.path:
    sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own imconfig FIRST; it puts the engine on sys.path in the right order.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

import imspine  # noqa: E402

import config  # noqa: E402
import people_db  # noqa: E402
import people_lint  # noqa: E402

# ---------------------------------------------------------------------------
# The member's real locations, captured at import — before any fixture patches them.
# ---------------------------------------------------------------------------

REAL_MEMORY_DIR: Path = config.MEMORY_DIR
REAL_PEOPLE_DIR: Path = config.PEOPLE_DIR
REAL_SNAPSHOTS_DIR: Path = config.MEMORY_SNAPSHOTS_DIR
REAL_LOCAL_SNAPSHOTS_DIR: Path = config.LOCAL_SNAPSHOTS_DIR
REAL_DATABASE_PATH: Path = config.DATABASE_PATH

#: The config attributes that are moved regardless of where they point.
_ALWAYS_MOVED = ("MEMORY_DIR", "PEOPLE_DIR", "MEMORY_SNAPSHOTS_DIR", "LOCAL_SNAPSHOTS_DIR",
                 "DATABASE_PATH")

#: Markers only this fixture writes, searched for in the real places afterwards.
FIXTURE_SLUG_PREFIX = "fixture-"
FIXTURE_NAME_PREFIX = "Fixture "
FIXTURE_EMAIL_DOMAIN = "fixture.example.com"
FIXTURE_PHONE_PREFIXES = ("+1555555014", "+1555555015", "+1555555016", "+1555555017",
                          "+1555555018", "+1555555019")


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardSpec:
    """One lint-clean fixture person card. Every field is invented."""

    slug: str
    name: str
    pid: str
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    shared: bool = False
    interactions_section: bool = True
    interaction_lines: tuple[str, ...] = ()
    category: str = "friend"
    tier: int = 2


@dataclass(frozen=True)
class Card:
    """A card the fixture wrote: its id, absolute path and vault-relative name."""

    slug: str
    pid: str
    path: Path
    rel: str


#: The standard cast. Ids are ``prs_`` + 8 characters from [a-z2-7], as the linter wants.
ALICE = CardSpec("fixture-alice", "Fixture Alice", "prs_fxalice2", phones=("+15555550142",))
BOB = CardSpec(
    "fixture-bob", "Fixture Bob", "prs_fxbobbb2",
    emails=(f"bob@{FIXTURE_EMAIL_DOMAIN}",),
    interaction_lines=("- 2026-08-30 — email (they_reached_out): An earlier fixture email",),
)
CAROL = CardSpec("fixture-carol", "Fixture Carol", "prs_fxcarol2", phones=("+15555550143",),
                 interactions_section=False)
DAN = CardSpec("fixture-dan", "Fixture Dan", "prs_fxdanxx2", phones=("+15555550150",),
               shared=True, category="family")
DEE = CardSpec("fixture-dee", "Fixture Dee", "prs_fxdeexx2", phones=("+15555550150",),
               shared=True, category="family")
ERIN = CardSpec("fixture-erin", "Fixture Erin", "prs_fxerinx2")
STANDARD_CARDS: tuple[CardSpec, ...] = (ALICE, BOB, CAROL, DAN, DEE, ERIN)

#: A number the ledger would see as a shared family landline: on two cards, marked shared.
SHARED_LANDLINE = "+15555550150"


def render_card(spec: CardSpec) -> str:
    """The card's markdown, in the shape ``people.cmd_new`` writes and a real card carries.

    Phones are JSON-quoted, as the engine quotes them (``people._append_identifier_item``):
    a bare ``+15555550142`` is a YAML integer and loses its ``+`` on load.
    """
    lines = [
        "---",
        f"id: {spec.pid}",
        f"slug: {spec.slug}",
        f"name: {spec.name}",
        f"category: {spec.category}",
        f"tier: {spec.tier}",
    ]
    shared = ", shared: true" if spec.shared else ""
    if spec.phones:
        lines.append("phones:")
        for i, phone in enumerate(spec.phones):
            primary = "true" if i == 0 else "false"
            lines.append(
                f'  - {{ value: "{phone}", type: mobile, primary: {primary}, active: true'
                f"{shared} }}"
            )
    if spec.emails:
        lines.append("emails:")
        for i, email in enumerate(spec.emails):
            primary = "true" if i == 0 else "false"
            lines.append(
                f"  - {{ value: {email}, type: home, primary: {primary}, active: true{shared} }}"
            )
    lines += [
        "source: manual",
        "confidence: low",
        'created: "2026-09-01T09:00:00-04:00"',
        "---",
        "",
        "## About",
        "",
    ]
    if spec.interactions_section:
        lines += ["## Interactions", *spec.interaction_lines, ""]
    lines += ["## Notes", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


def _within(child: Path, parent: Path) -> bool:
    """``child`` is ``parent`` or sits below it — lexical, on unresolved parts."""
    return child == parent or parent in child.parents


@contextlib.contextmanager
def patched(obj: Any, name: str, value: Any) -> Iterator[None]:
    """Set ``obj.name`` for the ``with`` block and put the old value back after."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@dataclass
class SpineFixture:
    """A temp vault + temp ``memory.db`` with every config location re-rooted.

    Use as a context manager, or ``start()`` / ``stop()``. ``conn`` is an
    ``imspine.open_conn`` connection on the temp database; a test that closes it can
    call :meth:`reopen`.
    """

    root: Path | None = None
    conn: sqlite3.Connection | None = None
    _saved: dict[str, Any] = field(default_factory=dict)

    # -- paths ---------------------------------------------------------------

    @property
    def memory_dir(self) -> Path:
        return self._root() / "Memory"

    @property
    def people_dir(self) -> Path:
        return self.memory_dir / "people"

    @property
    def snapshots_dir(self) -> Path:
        return self._root() / "memory-snapshots"

    @property
    def db_path(self) -> Path:
        return self._root() / "memory.db"

    def _root(self) -> Path:
        if self.root is None:
            raise RuntimeError("the fixture is not started")
        return self.root

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> SpineFixture:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> SpineFixture:
        """Make the temp tree, re-root config, prove it, open the temp database."""
        if config.MEMORY_DIR != REAL_MEMORY_DIR or config.DATABASE_PATH != REAL_DATABASE_PATH:
            raise RuntimeError(
                "config is already re-rooted (a fixture inside a fixture?); refusing, because "
                "stop() would then restore the wrong values"
            )
        # resolve(): macOS temp folders sit behind a /var -> /private/var symlink.
        self.root = Path(tempfile.mkdtemp(prefix="imspine-fixture-")).resolve()
        try:
            self.people_dir.mkdir(parents=True)
            self.snapshots_dir.mkdir()
            self._patch_config()
            self._assert_contained()
            self.conn = imspine.open_conn(db_path=self.db_path)
        except BaseException:
            self.stop()
            raise
        return self

    def _patch_config(self) -> None:
        moves: dict[str, Path] = {
            "MEMORY_SNAPSHOTS_DIR": self.snapshots_dir,
            "LOCAL_SNAPSHOTS_DIR": self._root() / "local-snapshots",
            "DATABASE_PATH": self.db_path,
        }
        for name, value in list(vars(config).items()):
            if name.startswith("__") or not isinstance(value, Path):
                continue
            if _within(value, REAL_MEMORY_DIR):
                moves[name] = self.memory_dir / value.relative_to(REAL_MEMORY_DIR)
        for name, new in moves.items():
            self._saved.setdefault(name, getattr(config, name))
            setattr(config, name, new)

    def _assert_contained(self) -> None:
        """Every moved location, and the five that must move, now sit under the temp root."""
        root = self._root()
        for name in {*self._saved, *_ALWAYS_MOVED}:
            value = getattr(config, name)
            if not _within(Path(value), root):
                raise RuntimeError(f"config.{name} still points outside the fixture: {value}")

    def stop(self) -> None:
        """Close, put every config value back exactly, remove the temp tree."""
        if self.conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self.conn.close()
            self.conn = None
        for name, value in self._saved.items():
            setattr(config, name, value)
        self._saved.clear()
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None

    def reopen(self) -> sqlite3.Connection:
        """Close the connection (NO commit — a close rolls back) and open a fresh one."""
        if self.conn is not None:
            self.conn.close()
        self.conn = imspine.open_conn(db_path=self.db_path)
        return self.conn

    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("the fixture has no open connection")
        return self.conn

    def commit(self) -> None:
        self.db().commit()

    # -- cards ---------------------------------------------------------------

    def write_card(self, spec: CardSpec, *, project: bool = True) -> Card:
        """Write a card (lint-gated, like every engine door) and, by default, project it."""
        text = render_card(spec)
        errors = [f.detail for f in people_lint.lint_text(text).flags if f.severity == "error"]
        if errors:
            raise AssertionError(f"fixture card {spec.slug} does not lint: {errors}")
        path = self.people_dir / f"{spec.slug}.md"
        path.write_text(text, encoding="utf-8")
        card = Card(spec.slug, spec.pid, path, f"people/{spec.slug}.md")
        if project:
            self.project([card.rel])
        return card

    def write_raw(self, name: str, text: str) -> Path:
        """A file in the people folder with exactly this text (for a malformed card)."""
        path = self.people_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def standard_cards(self) -> dict[str, Card]:
        """The standard cast, written and projected in one pass, committed."""
        cards = {spec.slug: self.write_card(spec, project=False) for spec in STANDARD_CARDS}
        self.project([c.rel for c in cards.values()])
        return cards

    def project(self, rels: list[str]) -> Any:
        """Project through imspine on the fixture connection, then COMMIT (G1's order)."""
        result = imspine.project(self.db(), rels)
        self.commit()
        if result.skipped:
            raise AssertionError(f"fixture projection skipped {result.skipped_paths}")
        return result

    # -- proposals, the engine's way -------------------------------------------

    def insert_proposal(
        self, kind: str, payload: dict[str, Any], *, person_id: str | None = None
    ) -> str:
        """``people_db.insert_proposal`` — with its payload-equality dedup (E3) — committed."""
        pid = people_db.insert_proposal(
            self.db(), kind, payload, created=config.now_local().isoformat(),
            person_id=person_id,
        )
        self.commit()
        return pid

    def dismiss(self, proposal_id: str, *, at: str | None = None) -> None:
        """Dismiss a proposal.

        ``at=None`` runs the engine's own door, ``people.cmd_proposal_set(id,
        "dismissed")`` — what ``people.py proposals dismiss`` runs — which flips the
        status and logs the ``change_log`` row the reopen guard reads, dated now.
        With ``at``, the fixture writes those same two things itself (the same calls,
        people.py:1777-1782) dated ``at``, so a test can place the dismissal on a day.
        """
        conn = self.db()
        if at is None:
            conn.commit()
            import people  # noqa: PLC0415 - heavy CLI module, only for the engine's door

            out = people.cmd_proposal_set(proposal_id, "dismissed")
            if out.get("status") != "ok":
                raise AssertionError(f"engine dismiss refused: {out}")
            return
        if not people_db.set_proposal_status(conn, proposal_id, "dismissed"):
            raise AssertionError(f"no proposal {proposal_id} to dismiss")
        people_db.log_change(
            conn, "person_proposal", proposal_id, "dismissed",
            f"proposal {proposal_id} dismissed", created=at,
        )
        conn.commit()

    def accept(self, proposal_id: str) -> dict[str, Any]:
        """``people.cmd_accept`` — the member's ``proposals accept`` — on the temp vault."""
        self.db().commit()
        import people  # noqa: PLC0415 - heavy CLI module, only for the engine's door

        out: dict[str, Any] = people.cmd_accept(proposal_id)
        return out


class SpineTestCase(unittest.TestCase):
    """A TestCase with a started fixture (``self.fx``) and the standard cast (``self.cards``)."""

    fx: SpineFixture
    cards: dict[str, Card]

    def setUp(self) -> None:
        super().setUp()
        self.fx = SpineFixture().start()
        self.addCleanup(self.fx.stop)
        self.cards = self.fx.standard_cards()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.fx.db()


# ---------------------------------------------------------------------------
# Proving nothing escaped
# ---------------------------------------------------------------------------


def _listing(folder: Path, *, recursive: bool) -> dict[str, tuple[int, int]]:
    """``{relative name: (mtime_ns, size)}`` for every file under ``folder``."""
    if not folder.is_dir():
        return {}
    files = folder.rglob("*") if recursive else folder.iterdir()
    out: dict[str, tuple[int, int]] = {}
    for f in files:
        with contextlib.suppress(OSError):
            if f.is_file():
                st = f.stat()
                out[f.relative_to(folder).as_posix()] = (st.st_mtime_ns, st.st_size)
    return out


def real_state(
    people_dir: Path | None = None, snapshots_dir: Path | None = None
) -> dict[str, dict[str, tuple[int, int]]]:
    """The member's real people folder and real undo ring, as listings. Reads only.

    The folders default to the REAL ones captured at import; a negative-control test
    passes temp stand-ins to prove the comparison bites.
    """
    return {
        "people": _listing(people_dir or REAL_PEOPLE_DIR, recursive=False),
        "snapshots": _listing(snapshots_dir or REAL_SNAPSHOTS_DIR, recursive=True),
    }


def real_fixture_traces() -> list[str]:
    """Anything of THIS fixture's found in the member's real places. Empty is clean.

    Looks for fixture-named cards and lock sidecars in the real people folder, undo
    rings whose ``.origin`` names a fixture card, and — through a READ-ONLY
    connection — fixture people or proposals in the real ``memory.db``. Reports what
    kind of trace it found, never a member's data.
    """
    traces: list[str] = []
    if REAL_PEOPLE_DIR.is_dir():
        for f in REAL_PEOPLE_DIR.iterdir():
            if f.name.startswith(FIXTURE_SLUG_PREFIX):
                traces.append(f"real people folder holds {f.name}")
    if REAL_SNAPSHOTS_DIR.is_dir():
        for ring in REAL_SNAPSHOTS_DIR.iterdir():
            origin = ring / ".origin"
            with contextlib.suppress(OSError):
                if origin.is_file() and origin.read_text(encoding="utf-8").startswith(
                    f"people/{FIXTURE_SLUG_PREFIX}"
                ):
                    traces.append(f"real undo ring holds {ring.name}")
    if REAL_DATABASE_PATH.is_file():
        try:
            ro = sqlite3.connect(f"{REAL_DATABASE_PATH.as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            return traces
        try:
            n_people = ro.execute(
                "SELECT COUNT(*) FROM person WHERE slug LIKE ?", (f"{FIXTURE_SLUG_PREFIX}%",)
            ).fetchone()[0]
            if n_people:
                traces.append(f"real memory.db holds {n_people} fixture person row(s)")
            clauses = ["payload LIKE ?"] * (2 + len(FIXTURE_PHONE_PREFIXES))
            params = [f"%{FIXTURE_NAME_PREFIX}%", f"%{FIXTURE_EMAIL_DOMAIN}%",
                      *(f"%{p}%" for p in FIXTURE_PHONE_PREFIXES)]
            n_props = ro.execute(
                f"SELECT COUNT(*) FROM person_proposal WHERE {' OR '.join(clauses)}", params
            ).fetchone()[0]
            if n_props:
                traces.append(f"real memory.db holds {n_props} fixture proposal row(s)")
        except sqlite3.Error:
            pass
        finally:
            ro.close()
    return traces
