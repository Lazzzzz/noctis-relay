"""Durable echo archive: sampled clips kept on disk under a quota per zone.

Sessions, rate limits and network fingerprints stay in memory (see store.py);
only clips of players who opted in are archived here, under the hash of a
random owner secret held by their game. The archive is a separate SQLite file
in WAL mode, written from the event-loop thread only.

Retention (schema 2, privacy revision 5):
- a *zone* is one spatial cell (``cell_size`` x ``cell_height`` x
  ``cell_size`` blocks of one realm and terrain profile). It keeps at most
  ``cell_max`` clips. A clip arriving in a full zone replaces the oldest clips
  *of that zone* that are at least ``floor`` old; if it has none, the clip is
  refused. A quiet zone is therefore never emptied for a busy one, and a visit
  younger than the floor is never discarded to make room;
- clips of owners who acknowledged revision 5 have no time limit unless the
  operator sets ``max_days``. Clips migrated from schema 1, and clips of
  sessions that did not acknowledge revision 5, keep the fixed lifetime of
  ``legacy_days`` that their notice promised (``expires`` column);
- the relay-wide row and byte caps never delete anything by themselves: a
  zone below its quota keeps all its clips and the new clip is refused while
  the archive is full. Only a full zone replaces (its own clips past the
  floor), and then takes enough of them to stay within the relay-wide caps.

Bounded by construction:
- admission samples at most one clip per owner every ``interval`` seconds and
  caps owners and zones per UTC day; making room in a full zone reads at
  most ``REPLACE_BATCH`` of its index entries;
- a lookup reads at most ``per_cell`` index entries in each non-empty
  neighbouring cell, starting from a random point of a random key drawn once
  per clip (a uniform sample of the cell whatever the hours people play, so a
  visit from two months ago is as likely as yesterday's), then picks a random
  person, then one of their clips. No full scan, no ORDER BY RANDOM();
- expiry deletes at most ``purge_batch`` rows per call, by an index holding
  only clips with a fixed lifetime (and, with ``max_days``, by creation time).

Failure is closed: an unreadable or unknown database refuses to start and is
never reset; a migration runs in one transaction; a runtime error disables
archiving and deletion requests answer 503 so that clients keep retrying
instead of believing their data is gone.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import sqlite3
import stat
import time
import zlib
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("noctis_relay")

SCHEMA_VERSION = 2
DAY = 86_400
# Privacy revision from which an owner's clips follow the zone quota.
QUOTA_REVISION = 5
# Oldest same-zone clips read when a full zone makes room.
REPLACE_BATCH = 16

# Empty counter rows, so that maintenance never scans every zone or owner.
COUNTERS = """
CREATE INDEX cell_empty ON cell(day) WHERE total = 0;
CREATE INDEX owner_empty ON owner(day) WHERE total = 0;
"""

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
-- One row per archived clip; small so that lookups read only index pages.
CREATE TABLE clip (
    id INTEGER PRIMARY KEY,
    realm TEXT NOT NULL,
    profile TEXT NOT NULL,
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    cy INTEGER NOT NULL,
    created INTEGER NOT NULL,      -- UTC unix seconds
    owner BLOB NOT NULL,           -- sha256(owner secret)[:16]
    account TEXT NOT NULL,
    instance BLOB NOT NULL,        -- truncated hash, as in memory
    x REAL NOT NULL,
    y REAL NOT NULL,
    z REAL NOT NULL,
    size INTEGER NOT NULL,
    shuffle INTEGER NOT NULL,      -- random 31-bit key: sampling order within a cell
    expires INTEGER                -- fixed end of life (UTC); NULL: kept under the zone quota
);
CREATE INDEX clip_cell ON clip(realm, profile, cx, cz, cy, shuffle);
CREATE INDEX clip_zone ON clip(realm, profile, cx, cz, cy, created);
CREATE INDEX clip_owner ON clip(owner);
CREATE INDEX clip_created ON clip(created);
CREATE INDEX clip_expires ON clip(expires) WHERE expires IS NOT NULL;
-- Compressed, validated clip and its separately removable chat lines.
CREATE TABLE body (id INTEGER PRIMARY KEY, body BLOB NOT NULL, chat TEXT);
-- Per-zone totals (lookup skips empty cells, quota) and today's admissions.
CREATE TABLE cell (
    realm TEXT NOT NULL, profile TEXT NOT NULL,
    cx INTEGER NOT NULL, cz INTEGER NOT NULL, cy INTEGER NOT NULL,
    total INTEGER NOT NULL, day INTEGER NOT NULL, today INTEGER NOT NULL,
    PRIMARY KEY (realm, profile, cx, cz, cy)
) WITHOUT ROWID;
-- Per-owner pacing and today's admissions.
CREATE TABLE owner (
    owner BLOB PRIMARY KEY,
    last INTEGER NOT NULL, day INTEGER NOT NULL, today INTEGER NOT NULL, total INTEGER NOT NULL
) WITHOUT ROWID;
""" + COUNTERS


class ArchiveError(Exception):
    """The archive cannot be opened safely; the relay must not start."""


@dataclass(frozen=True)
class Archived:
    id: int
    account: str
    created: int
    x: float
    y: float
    z: float
    body: str
    chat: str | None


def owner_hash(secret: str) -> bytes:
    # 256 random bits: a fast hash suffices, the archive never holds the secret.
    return hashlib.sha256(secret.encode("ascii")).digest()[:16]


def _migrate_1(db: sqlite3.Connection, legacy_days: int) -> int:
    """Schema 1 -> 2. Its clips were collected under a fixed-duration notice and keep it."""
    db.execute("ALTER TABLE clip ADD COLUMN expires INTEGER")
    n = db.execute("UPDATE clip SET expires = created + ?", (legacy_days * DAY,)).rowcount
    db.execute("CREATE INDEX clip_zone ON clip(realm, profile, cx, cz, cy, created)")
    db.execute("CREATE INDEX clip_expires ON clip(expires) WHERE expires IS NOT NULL")
    for statement in COUNTERS.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("UPDATE meta SET value = '2' WHERE key = 'schema'")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_from_1', ?), ('legacy_days', ?)",
               (str(int(time.time())), str(legacy_days)))
    return n


class Archive:
    def __init__(
        self,
        path: str,
        floor_days: int = 92,
        max_days: int = 0,
        legacy_days: int = 92,
        cell_size: int = 32,
        cell_height: int = 16,
        radius: int = 48,
        vertical: int = 16,
        interval: int = 300,
        owner_daily: int = 48,
        cell_daily: int = 24,
        cell_max: int = 4096,
        max_clips: int = 4_000_000,
        max_bytes: int = 16 * 1024 ** 3,
        min_age: int = 60,
        per_cell: int = 8,
        check: str = "off",
        wallclock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        if max_days and max_days < floor_days:
            raise ArchiveError("maximum age below the age floor")
        self.path = path
        self.floor = floor_days * DAY
        self.max_age = max_days * DAY
        self.legacy = legacy_days * DAY
        self.cell_size, self.cell_height = cell_size, cell_height
        self.radius, self.vertical = radius, vertical
        self.interval = interval
        self.owner_daily, self.cell_daily, self.cell_max = owner_daily, cell_daily, cell_max
        self.max_clips, self.max_bytes = max_clips, max_bytes
        self.min_age = min_age
        self.per_cell = per_cell
        self.wallclock = wallclock
        self.rng = rng or random.SystemRandom()
        self.failed = False
        self.span = math.ceil(radius / cell_size)
        self.db = self._open(path, check, legacy_days)
        self.rows = self.db.execute("SELECT count(*) FROM clip").fetchone()[0] if path else 0
        self.reseed_ids()

    def reseed_ids(self) -> None:
        """Next clip id, above every id in the file (one lookup at the end of the rowid tree).

        Ids are never reused while the relay runs, even once the newest clips
        were deleted: delivery records in memory hold archive ids, and a
        reused one would hide a new clip from observers who saw the old one.
        Delivery records start empty at boot, so the file maximum suffices.
        Call again after writing clips to the file directly (benchmarks).
        """
        self.next_id = (self.db.execute("SELECT max(id) FROM clip").fetchone()[0] or 0) + 1

    # -- opening ----------------------------------------------------------

    @staticmethod
    def _open(path: str, check: str, legacy_days: int) -> sqlite3.Connection:
        fresh = not os.path.exists(path)
        if fresh:
            directory = os.path.dirname(os.path.abspath(path))
            if not os.path.isdir(directory):
                raise ArchiveError("archive directory does not exist")
            # Created private before SQLite touches it (WAL/SHM inherit the mode).
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError as exc:
                raise ArchiveError(f"cannot create archive ({type(exc).__name__})") from None
            os.close(fd)
        elif not stat.S_ISREG(os.stat(path).st_mode):
            raise ArchiveError("archive path is not a regular file")
        try:
            db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            db.execute("PRAGMA busy_timeout=2000")
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise ArchiveError("archive must support WAL")
            # FULL: a deletion acknowledged to a player survives a power loss.
            db.execute("PRAGMA synchronous=FULL")
            # Overwrite deleted content instead of leaving it in free pages.
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA foreign_keys=OFF")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                if not fresh and os.path.getsize(path) > 0:
                    raise ArchiveError("archive file is not empty but has no tables")
                # One transaction: a crash never leaves a half-created schema behind.
                db.executescript(
                    "BEGIN;" + SCHEMA
                    + f"INSERT INTO meta VALUES ('schema', '{SCHEMA_VERSION}'), "
                    f"('created', '{int(time.time())}'); COMMIT;")
            else:
                if "meta" not in tables:
                    raise ArchiveError("archive has no version: refusing to touch it")
                row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
                version = int(row[0]) if row and row[0].isdigit() else -1
                if version == 1:
                    # One transaction: a crash or error leaves schema 1 untouched.
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        n = _migrate_1(db, legacy_days)
                        db.execute("COMMIT")
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                    log.info("archive migrated to schema 2: %d earlier clips keep a fixed %d-day lifetime",
                             n, legacy_days)
                    version = 2
                if version != SCHEMA_VERSION:
                    raise ArchiveError(f"unsupported archive schema {version}")
            if check == "quick":
                result = db.execute("PRAGMA quick_check(1)").fetchone()[0]
                if result != "ok":
                    raise ArchiveError("archive failed quick_check")
            return db
        except sqlite3.DatabaseError as exc:
            raise ArchiveError(f"archive unreadable ({type(exc).__name__})") from None

    def _fail(self, exc: BaseException) -> None:
        if not self.failed:
            log.error("archive disabled after %s; restart after checking the database", type(exc).__name__)
        self.failed = True

    # -- helpers ----------------------------------------------------------

    def cell_of(self, x: float, y: float, z: float) -> tuple[int, int, int]:
        return (math.floor(x / self.cell_size), math.floor(z / self.cell_size), math.floor(y / self.cell_height))

    def now(self) -> int:
        return int(self.wallclock())

    def size_bytes(self) -> int:
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * self.db.execute("PRAGMA page_size").fetchone()[0]

    # -- admission --------------------------------------------------------

    _ZONE_OLDEST = (
        "SELECT id, realm, profile, cx, cz, cy, owner, size FROM clip INDEXED BY clip_zone "
        "WHERE realm = ? AND profile = ? AND cx = ? AND cz = ? AND cy = ? AND created <= ? "
        "ORDER BY created LIMIT ?"
    )

    def _room(self, zone: tuple, total: int, size: int, now: int) -> list | None:
        """Clips of ``zone`` to replace so that a new one fits, or None if it cannot.

        Only a full zone replaces, and only its own clips at least ``floor``
        old, oldest first. A zone below its quota is never thinned out: when
        a relay-wide cap blocks it, the clip is refused. While a cap is
        reached the zone total, the row count and (approximately) the byte
        size never grow.
        """
        zone_need = total + 1 - self.cell_max
        rows_need = self.rows + 1 - self.max_clips
        over_bytes = self.size_bytes() + size - self.max_bytes
        if zone_need <= 0:
            return [] if rows_need <= 0 and over_bytes <= 0 else None
        rows = self.db.execute(self._ZONE_OLDEST, (*zone, now - self.floor, REPLACE_BATCH)).fetchall()
        if not rows:
            return None
        need = max(zone_need, rows_need)
        if over_bytes > 0:
            freed = 0
            for i, row in enumerate(rows):
                freed += row[7]
                if freed >= over_bytes:
                    need = max(need, i + 1)
                    break
            else:
                return None
        # A cap lowered by the operator converges by at most REPLACE_BATCH per admission.
        return rows[:need]

    def admit(
        self, owner: bytes, account: str, realm: str, profile: str, instance: bytes,
        x: float, y: float, z: float, body: str, chat: str | None, fixed: bool = True,
    ) -> int | None:
        """Archive a clip if sampling and quotas allow it; returns its id or None.

        ``fixed``: the owner did not acknowledge zone retention, so the clip
        expires after ``legacy_days`` like under the earlier notice.
        """
        if self.failed:
            return None
        now = self.now()
        day = now // DAY
        cx, cz, cy = self.cell_of(x, y, z)
        zone = (realm, profile, cx, cz, cy)
        packed = zlib.compress(body.encode("utf-8"), 6)
        size = len(packed) + (len(chat) if chat else 0) + 160
        db = self.db
        try:
            o = db.execute("SELECT last, day, today FROM owner WHERE owner = ?", (owner,)).fetchone()
            if o is not None:
                if now - o[0] < self.interval:
                    return None
                if o[1] == day and o[2] >= self.owner_daily:
                    return None
            c = db.execute(
                "SELECT total, day, today FROM cell WHERE realm = ? AND profile = ? AND cx = ? AND cz = ? AND cy = ?",
                zone,
            ).fetchone()
            if c is not None and c[1] == day and c[2] >= self.cell_daily:
                return None
            victims = self._room(zone, c[0] if c is not None else 0, size, now)
            if victims is None:
                return None
            db.execute("BEGIN")
            try:
                if victims:
                    self._remove(victims)
                cid = self.next_id
                db.execute(
                    "INSERT INTO clip(id, realm, profile, cx, cz, cy, created, owner, account, instance, x, y, z, "
                    "size, shuffle, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, *zone, now, owner, account, instance, x, y, z, size,
                     self.rng.getrandbits(31), now + self.legacy if fixed else None),
                )
                db.execute("INSERT INTO body(id, body, chat) VALUES (?, ?, ?)", (cid, packed, chat))
                db.execute(
                    "INSERT INTO owner(owner, last, day, today, total) VALUES (?, ?, ?, 1, 1) "
                    "ON CONFLICT(owner) DO UPDATE SET last = excluded.last, total = total + 1, "
                    "today = CASE WHEN day = excluded.day THEN today + 1 ELSE 1 END, day = excluded.day",
                    (owner, now, day),
                )
                db.execute(
                    "INSERT INTO cell(realm, profile, cx, cz, cy, total, day, today) VALUES (?, ?, ?, ?, ?, 1, ?, 1) "
                    "ON CONFLICT(realm, profile, cx, cz, cy) DO UPDATE SET total = total + 1, "
                    "today = CASE WHEN day = excluded.day THEN today + 1 ELSE 1 END, day = excluded.day",
                    (*zone, day),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        except sqlite3.DatabaseError as exc:
            self._fail(exc)
            return None
        self.rows += 1 - len(victims)
        self.next_id = cid + 1
        return cid

    # -- lookup -----------------------------------------------------------

    def _cells(self, realm, profile, x, y, z) -> list[tuple[int, int, int]]:
        cx, cz, _ = self.cell_of(x, y, z)
        lo = math.floor((y - self.vertical) / self.cell_height)
        hi = math.floor((y + self.vertical) / self.cell_height)
        xs = [cx + d for d in range(-self.span, self.span + 1)]
        zs = [cz + d for d in range(-self.span, self.span + 1)]
        ys = list(range(lo, hi + 1))
        marks = lambda n: ", ".join("?" * n)  # noqa: E731
        return [
            (r[0], r[1], r[2]) for r in self.db.execute(
                "SELECT cx, cz, cy FROM cell WHERE realm = ? AND profile = ? "
                f"AND cx IN ({marks(len(xs))}) AND cz IN ({marks(len(zs))}) AND cy IN ({marks(len(ys))}) "
                "AND total > 0",
                (realm, profile, *xs, *zs, *ys),
            )
        ]

    _PAGE = (
        "SELECT id, owner, account, instance, x, y, z, created, expires FROM clip INDEXED BY clip_cell "
        "WHERE realm = ? AND profile = ? AND cx = ? AND cz = ? AND cy = ? AND shuffle >= ? AND shuffle < ? "
        "ORDER BY shuffle LIMIT ?"
    )

    def pick(
        self, realm: str, profile: str, x: float, y: float, z: float,
        exclude_owner: bytes | None, exclude_account: str | None, exclude_instance: bytes,
        delivered: Callable[[int], bool],
    ) -> Archived | None:
        """An old visit near (x, y, z) from another game: random sample of each cell, random person."""
        if self.failed:
            return None
        now = self.now()
        newest = now - self.min_age
        oldest = now - self.max_age if self.max_age else None
        pivot = self.rng.getrandbits(31)
        r2, v = float(self.radius * self.radius), float(self.vertical)
        try:
            by_account: dict[str, list] = {}
            for cx, cz, cy in self._cells(realm, profile, x, y, z):
                key = (realm, profile, cx, cz, cy)
                rows = self.db.execute(self._PAGE, (*key, pivot, 1 << 31, self.per_cell)).fetchall()
                if len(rows) < self.per_cell:  # wrap around
                    rows += self.db.execute(
                        self._PAGE, (*key, 0, pivot, self.per_cell - len(rows))).fetchall()
                for cid, owner, account, instance, cxp, cyp, czp, created, expires in rows:
                    if owner == exclude_owner or account == exclude_account or instance == exclude_instance:
                        continue
                    # Still served live, or past a fixed or maximum age but not yet purged.
                    if created > newest or (expires is not None and expires <= now):
                        continue
                    if oldest is not None and created < oldest:
                        continue
                    if (cxp - x) ** 2 + (czp - z) ** 2 > r2 or abs(cyp - y) > v or delivered(cid):
                        continue
                    by_account.setdefault(account, []).append((cid, account, created, cxp, cyp, czp))
            if not by_account:
                return None
            account = self.rng.choice(sorted(by_account))
            cid, account, created, cxp, cyp, czp = self.rng.choice(by_account[account])
            row = self.db.execute("SELECT body, chat FROM body WHERE id = ?", (cid,)).fetchone()
        except sqlite3.DatabaseError as exc:
            self._fail(exc)
            return None
        if row is None:
            return None
        return Archived(cid, account, created, cxp, cyp, czp, zlib.decompress(row[0]).decode("utf-8"), row[1])

    _ROWS = "SELECT id, realm, profile, cx, cz, cy, owner FROM clip "
    _EXPIRED = _ROWS + "INDEXED BY clip_expires WHERE expires IS NOT NULL AND expires <= ? ORDER BY expires LIMIT ?"
    _TOO_OLD = _ROWS + "INDEXED BY clip_created WHERE created < ? ORDER BY created LIMIT ?"
    _IDLE_OWNERS = ("DELETE FROM owner WHERE owner IN (SELECT owner FROM owner INDEXED BY owner_empty "
                    "WHERE total = 0 AND day < ? LIMIT ?)")
    _EMPTY_CELLS = ("DELETE FROM cell WHERE (realm, profile, cx, cz, cy) IN (SELECT realm, profile, cx, cz, cy "
                    "FROM cell INDEXED BY cell_empty WHERE total = 0 AND day < ? LIMIT ?)")

    def explain(self) -> list[str]:
        plans = []
        for sql, params in (
            (self._PAGE, ("r", "p", 0, 0, 0, 0, 1 << 31, 8)),
            (self._ZONE_OLDEST, ("r", "p", 0, 0, 0, 0, REPLACE_BATCH)),
            (self._EXPIRED, (0, 10)),
            (self._TOO_OLD, (0, 10)),
            (self._ROWS + "WHERE owner = ?", (b"",)),
            (self._IDLE_OWNERS, (0, 10)),
            (self._EMPTY_CELLS, (0, 10)),
        ):
            plans += [r[3] for r in self.db.execute("EXPLAIN QUERY PLAN " + sql, params)]
        return plans

    # -- deletion and expiry ----------------------------------------------

    def _remove(self, rows: list) -> None:
        """Delete clips (id, realm, profile, cx, cz, cy, owner, ...) and keep counters exact."""
        db = self.db
        cells: dict = {}
        owners: dict = {}
        for cid, realm, profile, cx, cz, cy, owner, *_ in rows:
            cells[(realm, profile, cx, cz, cy)] = cells.get((realm, profile, cx, cz, cy), 0) + 1
            owners[owner] = owners.get(owner, 0) + 1
        db.executemany("DELETE FROM body WHERE id = ?", ((r[0],) for r in rows))
        db.executemany("DELETE FROM clip WHERE id = ?", ((r[0],) for r in rows))
        for key, n in cells.items():
            db.execute("UPDATE cell SET total = max(0, total - ?) WHERE realm = ? AND profile = ? AND cx = ? "
                       "AND cz = ? AND cy = ?", (n, *key))
        for owner, n in owners.items():
            db.execute("UPDATE owner SET total = max(0, total - ?) WHERE owner = ?", (n, owner))

    def forget(self, owner: bytes, chat_only: bool) -> int:
        """Private deletion by owner. Raises ArchiveError if the archive is unavailable."""
        if self.failed:
            raise ArchiveError("archive unavailable")
        db = self.db
        try:
            db.execute("BEGIN")
            try:
                if chat_only:
                    n = db.execute(
                        "UPDATE body SET chat = NULL WHERE chat IS NOT NULL AND id IN "
                        "(SELECT id FROM clip WHERE owner = ?)", (owner,)).rowcount
                else:
                    rows = db.execute(self._ROWS + "WHERE owner = ?", (owner,)).fetchall()
                    self._remove(rows)
                    db.execute("DELETE FROM owner WHERE owner = ?", (owner,))
                    n = len(rows)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            if not chat_only:
                self.rows -= n
            # Do not leave the removed pages in the write-ahead log either.
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError as exc:
            self._fail(exc)
            raise ArchiveError("archive unavailable") from None
        return n

    def purge(self, limit: int = 200) -> int:
        """Remove at most ``limit`` clips past their fixed lifetime or the maximum age.

        Clips kept under the zone quota are never touched here: a quiet zone
        keeps its old visits, only new clips of the same zone replace them.
        """
        if self.failed:
            return 0
        now = self.now()
        db = self.db
        try:
            rows = db.execute(self._EXPIRED, (now, limit)).fetchall()
            if self.max_age and len(rows) < limit:
                seen = {r[0] for r in rows}
                rows += [r for r in db.execute(self._TOO_OLD, (now - self.max_age, limit - len(rows)))
                         if r[0] not in seen]
            if rows:
                db.execute("BEGIN")
                try:
                    self._remove(rows)
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                self.rows -= len(rows)
            # Counter rows of quiet owners/cells, bounded as well.
            day = now // DAY
            db.execute(self._IDLE_OWNERS, (day - 1, limit))
            db.execute(self._EMPTY_CELLS, (day - 1, limit))
        except sqlite3.DatabaseError as exc:
            self._fail(exc)
            return 0
        return len(rows)

    def stats(self) -> dict:
        return {"clips": self.rows, "bytes": self.size_bytes(), "failed": self.failed}

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass
