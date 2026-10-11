"""In-memory SQLite store for sessions, optional presence and echo clips.

Nothing is written to disk: the database lives in process memory and is
lost on restart, which is the intended retention policy. Clips of opted-in
owners may additionally be sampled into the durable archive (archive.py). All access happens
from the single event-loop thread, so one connection is enough and there is
no writer contention.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

SCHEMA = """
CREATE TABLE session (
    sid INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash BLOB NOT NULL UNIQUE,
    expires REAL NOT NULL,
    presence INTEGER NOT NULL DEFAULT 0,
    last_presence REAL NOT NULL DEFAULT -1e300,
    -- Keyed fingerprint of the client network that last used the session
    -- (never an address; the key is random per process). Overwritten, not
    -- appended: no history is kept.
    net BLOB NOT NULL DEFAULT x'',
    -- Granted capabilities and per-session pacing of echo routes.
    echoes INTEGER NOT NULL DEFAULT 0,
    chat INTEGER NOT NULL DEFAULT 0,
    last_publish REAL NOT NULL DEFAULT -1e300,
    last_fetch REAL NOT NULL DEFAULT -1e300,
    -- Hash of the last accepted chat line (duplicate filter), overwritten.
    last_chat BLOB NOT NULL DEFAULT x'',
    -- Hash of the owner secret (archive and private deletion), and the
    -- account the client declared (only used to exclude its own replays).
    owner BLOB,
    account TEXT NOT NULL DEFAULT ''
);
CREATE INDEX session_expires ON session(expires);
CREATE INDEX session_owner ON session(owner) WHERE owner IS NOT NULL;
CREATE INDEX session_net ON session(net, expires);

-- Clustered on the spatial key: a neighbour lookup is a handful of B-tree
-- seeks (one per horizontal cell) followed by a short range on cy.
CREATE TABLE presence (
    realm TEXT NOT NULL,
    profile TEXT NOT NULL,
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    cy INTEGER NOT NULL,
    sid INTEGER NOT NULL,
    instance BLOB NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    z REAL NOT NULL,
    expires REAL NOT NULL,
    PRIMARY KEY (realm, profile, cx, cz, cy, sid)
) WITHOUT ROWID;
CREATE UNIQUE INDEX presence_sid ON presence(sid);
CREATE INDEX presence_expires ON presence(expires);

-- At most one short echo clip per session, clustered like presence on its
-- origin. ``body`` is the validated, re-serialised clip (never client bytes);
-- ``chat`` holds the accepted chat lines separately, served only to
-- observers whose own session was granted the chat capability.
CREATE TABLE clip (
    realm TEXT NOT NULL,
    profile TEXT NOT NULL,
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    cy INTEGER NOT NULL,
    sid INTEGER NOT NULL,
    clip_id INTEGER NOT NULL,
    instance BLOB NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    z REAL NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    body TEXT NOT NULL,
    chat TEXT,
    -- Archive row sampled from this clip, so an observer does not get it twice.
    archive_id INTEGER,
    PRIMARY KEY (realm, profile, cx, cz, cy, sid)
) WITHOUT ROWID;
CREATE UNIQUE INDEX clip_sid ON clip(sid);
CREATE UNIQUE INDEX clip_id ON clip(clip_id);
CREATE INDEX clip_expires ON clip(expires);

-- Clips already handed to an observer, so each is replayed at most once
-- (archived clips are recorded under their negated archive id).
CREATE TABLE delivered (
    observer INTEGER NOT NULL,
    clip_id INTEGER NOT NULL,
    expires REAL NOT NULL,
    PRIMARY KEY (observer, clip_id)
) WITHOUT ROWID;
CREATE INDEX delivered_expires ON delivered(expires);
"""


# How long an observer's session remembers an archived clip it was shown.
ARCHIVE_SEEN = 6 * 3600.0


def hash_token(token: str) -> bytes:
    # Tokens carry 256 random bits, so a fast hash is sufficient: the goal is
    # that a memory dump of the database does not reveal usable bearer tokens.
    return hashlib.sha256(token.encode("ascii")).digest()


@dataclass(frozen=True)
class Session:
    sid: int
    expires: float
    presence: bool
    last_presence: float
    net: bytes = b""
    echoes: bool = False
    chat: bool = False
    last_publish: float = -1e300
    last_fetch: float = -1e300
    last_chat: bytes = b""
    owner: bytes | None = None
    account: str = ""


@dataclass(frozen=True)
class Neighbour:
    ref: str
    x: float
    y: float
    z: float
    distance: float


@dataclass(frozen=True)
class Echo:
    ref: str
    age: float
    x: float
    y: float
    z: float
    body: str
    chat: str | None
    archive_id: int | None = None


class CapacityError(Exception):
    pass


class AreaFullError(Exception):
    """The target presence cell already holds its maximum of live positions."""


class Store:
    def __init__(
        self,
        retention_seconds: int,
        max_sessions: int,
        cell_size: int = 32,
        cell_height: int = 16,
        near_radius: int = 48,
        near_vertical: int = 12,
        near_limit: int = 8,
        clock: Callable[[], float] = time.monotonic,
        max_per_cell: int = 0,
        echo_ttl: int = 60,
        echo_radius: int = 48,
        echo_vertical: int = 16,
        echo_max_per_cell: int = 0,
    ) -> None:
        self.retention = float(retention_seconds)
        self.max_sessions = max_sessions
        self.cell_size = cell_size
        self.cell_height = cell_height
        self.near_radius = near_radius
        self.near_vertical = near_vertical
        self.near_limit = near_limit
        self.max_per_cell = max_per_cell
        self.clock = clock
        self._ref_key = secrets.token_bytes(32)
        self.db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.executescript(SCHEMA)
        self._session_rows = 0
        # Number of cells probed on each horizontal axis around the query.
        self._span = math.ceil(near_radius / cell_size)
        self._near_sql = self._build_near_sql()
        self.echo_ttl = float(echo_ttl)
        self.echo_radius = echo_radius
        self.echo_vertical = echo_vertical
        self.echo_max_per_cell = echo_max_per_cell
        self._echo_span = math.ceil(echo_radius / cell_size)
        self._echo_sql = self._build_echo_sql()
        self._clip_ids = 0

    # -- sessions ---------------------------------------------------------

    def session_count(self) -> int:
        return self._session_rows

    def network_sessions(self, net: bytes) -> int:
        """Live sessions attributed to ``net`` (index range, no table scan)."""
        return self.db.execute(
            "SELECT count(*) FROM session WHERE net = ? AND expires > ?", (net, self.clock())
        ).fetchone()[0]

    def create_session(
        self, presence: bool = False, net: bytes = b"", echoes: bool = False, chat: bool = False,
        owner: bytes | None = None, account: str = "",
    ) -> tuple[str, Session]:
        now = self.clock()
        if self._session_rows >= self.max_sessions:
            # Reclaim a bounded batch even at capacity; periodic maintenance
            # drains any larger backlog without monopolising the event loop.
            self.purge(now)
            if self._session_rows >= self.max_sessions:
                raise CapacityError()
        token = secrets.token_hex(32)
        expires = now + self.retention
        cur = self.db.execute(
            "INSERT INTO session(token_hash, expires, presence, net, echoes, chat, owner, account) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (hash_token(token), expires, 1 if presence else 0, net, int(echoes), int(chat), owner, account),
        )
        self._session_rows += 1
        return token, Session(cur.lastrowid, expires, presence, -1e300, net, echoes, chat,
                              owner=owner, account=account)

    def find_session(self, token: str) -> Session | None:
        row = self.db.execute(
            "SELECT sid, expires, presence, last_presence, net, echoes, chat, last_publish, last_fetch, "
            "last_chat, owner, account FROM session WHERE token_hash = ?",
            (hash_token(token),),
        ).fetchone()
        if row is None or row[1] <= self.clock():
            return None
        return Session(row[0], row[1], bool(row[2]), row[3], row[4], bool(row[5]), bool(row[6]),
                       row[7], row[8], row[9], row[10], row[11])

    def refresh_session(self, session: Session, net: bytes | None = None) -> Session | None:
        """Extend a live session and attribute it to ``net`` (if given)."""
        now = self.clock()
        expires = now + self.retention
        net = session.net if net is None else net
        row = self.db.execute(
            "UPDATE session SET expires = ?, net = ? WHERE sid = ? AND expires > ? "
            "RETURNING sid",
            (expires, net, session.sid, now),
        ).fetchone()
        if row is None:
            return None
        return dataclasses.replace(session, expires=expires, net=net)

    def forget_owner(self, owner: bytes, chat_only: bool) -> int:
        """Private deletion: end every live session of ``owner`` (or only its chat)."""
        db = self.db
        db.execute("BEGIN")
        try:
            sids = [r[0] for r in db.execute("SELECT sid FROM session WHERE owner = ?", (owner,))]
            if chat_only:
                db.execute("UPDATE session SET chat = 0 WHERE owner = ?", (owner,))
                db.executemany("UPDATE clip SET chat = NULL WHERE sid = ?", ((sid,) for sid in sids))
            else:
                for sid in sids:
                    db.execute("DELETE FROM presence WHERE sid = ?", (sid,))
                    self._forget_echoes(sid)
                db.executemany("DELETE FROM session WHERE sid = ?", ((sid,) for sid in sids))
                self._session_rows -= len(sids)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return len(sids)

    def delete_session(self, token: str) -> bool:
        db = self.db
        db.execute("BEGIN")
        try:
            row = db.execute(
                "DELETE FROM session WHERE token_hash = ? RETURNING sid", (hash_token(token),)
            ).fetchone()
            if row is not None:
                db.execute("DELETE FROM presence WHERE sid = ?", (row[0],))
                self._forget_echoes(row[0])
                self._session_rows -= 1
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return row is not None

    # -- presence ---------------------------------------------------------

    def cell_of(self, x: float, y: float, z: float) -> tuple[int, int, int]:
        # floor() keeps negative coordinates in the right cell (-1 -> -1, not 0).
        return (
            math.floor(x / self.cell_size),
            math.floor(z / self.cell_size),
            math.floor(y / self.cell_height),
        )

    def _build_near_sql(self) -> str:
        n = 2 * self._span + 1
        cx = ", ".join("?" * n)
        cz = ", ".join("?" * n)
        return (
            "SELECT sid, x, y, z, (x - ?) * (x - ?) + (z - ?) * (z - ?) AS d2 "
            "FROM presence "
            f"WHERE realm = ? AND profile = ? AND cx IN ({cx}) AND cz IN ({cz}) "
            "AND cy BETWEEN ? AND ? "
            "AND expires > ? AND sid != ? AND instance != ? "
            "AND d2 <= ? AND abs(y - ?) <= ? "
            "ORDER BY d2 LIMIT ?"
        )

    def _near_params(self, realm, profile, x, y, z, now, sid, instance) -> list:
        cx, cz, _ = self.cell_of(x, y, z)
        span = range(-self._span, self._span + 1)
        cy_lo = math.floor((y - self.near_vertical) / self.cell_height)
        cy_hi = math.floor((y + self.near_vertical) / self.cell_height)
        return [
            x, x, z, z,
            realm, profile,
            *(cx + d for d in span),
            *(cz + d for d in span),
            cy_lo, cy_hi,
            now, sid, instance,
            float(self.near_radius * self.near_radius), y, float(self.near_vertical),
            self.near_limit,
        ]

    def update_presence(
        self,
        session: Session,
        realm: str,
        profile: str,
        instance: str,
        x: float,
        y: float,
        z: float,
        net: bytes | None = None,
    ) -> tuple[float, list[Neighbour]]:
        """Upsert the caller's position, refresh its session and return neighbours.

        Raises AreaFullError, without any change, if the position enters a
        cell that already holds ``max_per_cell`` live positions.
        """
        now = self.clock()
        net = session.net if net is None else net
        expires = now + self.retention
        inst = hashlib.sha256(instance.encode("ascii")).digest()[:16]
        cx, cz, cy = self.cell_of(x, y, z)
        db = self.db
        db.execute("BEGIN")
        try:
            db.execute(
                "UPDATE session SET expires = ?, last_presence = ?, net = ? WHERE sid = ?",
                (expires, now, net, session.sid),
            )
            cur = db.execute(
                "UPDATE presence SET x = ?, y = ?, z = ?, expires = ?, instance = ? "
                "WHERE sid = ? AND realm = ? AND profile = ? AND cx = ? AND cz = ? AND cy = ?",
                (x, y, z, expires, inst, session.sid, realm, profile, cx, cz, cy),
            )
            if cur.rowcount == 0:
                if self.max_per_cell and db.execute(
                    "SELECT count(*) FROM (SELECT 1 FROM presence WHERE realm = ? AND profile = ? "
                    "AND cx = ? AND cz = ? AND cy = ? AND expires > ? LIMIT ?)",
                    (realm, profile, cx, cz, cy, now, self.max_per_cell),
                ).fetchone()[0] >= self.max_per_cell:
                    raise AreaFullError()
                db.execute("DELETE FROM presence WHERE sid = ?", (session.sid,))
                db.execute(
                    "INSERT INTO presence(realm, profile, cx, cz, cy, sid, instance, x, y, z, expires) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (realm, profile, cx, cz, cy, session.sid, inst, x, y, z, expires),
                )
            rows = db.execute(
                self._near_sql, self._near_params(realm, profile, x, y, z, now, session.sid, inst)
            ).fetchall()
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return expires, [
            Neighbour(self._ref(session.sid, r[0]), r[1], r[2], r[3], math.sqrt(r[4])) for r in rows
        ]

    def clear_presence(self, session: Session) -> None:
        self.db.execute("DELETE FROM presence WHERE sid = ?", (session.sid,))

    def _ref(self, observer: int, other: int) -> str:
        # Opaque per-observer handle: stable while both sessions live, useless
        # to correlate across observers and never exposes internal ids.
        msg = observer.to_bytes(8, "big") + other.to_bytes(8, "big")
        return hmac.new(self._ref_key, msg, hashlib.sha256).hexdigest()[:16]

    def explain_near(self) -> list[str]:
        params = self._near_params("r", "p", 0.0, 64.0, 0.0, 0.0, 0, b"")
        return [row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + self._near_sql, params)]

    # -- echoes -----------------------------------------------------------

    def _forget_echoes(self, sid: int) -> None:
        """Drop a session's clip and its delivery history (inside a transaction)."""
        self.db.execute("DELETE FROM clip WHERE sid = ?", (sid,))
        self.db.execute("DELETE FROM delivered WHERE observer = ?", (sid,))

    def withdraw_echoes(self, session: Session) -> None:
        db = self.db
        db.execute("BEGIN")
        try:
            self._forget_echoes(session.sid)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _build_echo_sql(self) -> str:
        n = 2 * self._echo_span + 1
        marks = ", ".join("?" * n)
        # One B-tree seek per neighbouring horizontal cell; delivery exclusion
        # is a primary-key probe per candidate row.
        return (
            "SELECT sid, clip_id, x, y, z, created, body, chat, archive_id, "
            "(x - ?) * (x - ?) + (z - ?) * (z - ?) AS d2 FROM clip "
            f"WHERE realm = ? AND profile = ? AND cx IN ({marks}) AND cz IN ({marks}) "
            "AND cy BETWEEN ? AND ? AND expires > ? AND sid != ? AND instance != ? "
            "AND d2 <= ? AND abs(y - ?) <= ? "
            "AND NOT EXISTS (SELECT 1 FROM delivered d WHERE d.observer = ? AND d.clip_id = clip.clip_id) "
            # The source session must still be live: an expired one is excluded
            # at once, without waiting for the purge batch (rowid probe).
            "AND EXISTS (SELECT 1 FROM session s WHERE s.sid = clip.sid AND s.expires > ?) "
            "ORDER BY d2 LIMIT 1"
        )

    def _echo_params(self, realm, profile, x, y, z, now, sid, inst) -> list:
        cx, cz, _ = self.cell_of(x, y, z)
        span = range(-self._echo_span, self._echo_span + 1)
        return [
            x, x, z, z, realm, profile,
            *(cx + d for d in span), *(cz + d for d in span),
            math.floor((y - self.echo_vertical) / self.cell_height),
            math.floor((y + self.echo_vertical) / self.cell_height),
            now, sid, inst,
            float(self.echo_radius * self.echo_radius), y, float(self.echo_vertical),
            sid, now,
        ]

    def link_archive(self, session: Session, archive_id: int) -> None:
        self.db.execute("UPDATE clip SET archive_id = ? WHERE sid = ?", (archive_id, session.sid))

    def was_delivered(self, session: Session, archive_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM delivered WHERE observer = ? AND clip_id = ?", (session.sid, -archive_id)
        ).fetchone() is not None

    def mark_delivered(self, session: Session, archive_id: int, seconds: float) -> None:
        """Remember an archived clip for this observer's session (bounded by its lifetime)."""
        self.db.execute(
            "INSERT OR IGNORE INTO delivered(observer, clip_id, expires) VALUES (?, ?, ?)",
            (session.sid, -archive_id, self.clock() + seconds),
        )

    def touch_fetch(self, session: Session, net: bytes | None = None) -> None:
        now = self.clock()
        self.db.execute(
            "UPDATE session SET expires = ?, last_fetch = ?, net = ? WHERE sid = ?",
            (now + self.retention, now, session.net if net is None else net, session.sid),
        )

    def archived_ref(self, session: Session, archive_id: int) -> str:
        return self._ref(session.sid, (1 << 62) + archive_id)

    @staticmethod
    def instance_key(instance: str) -> bytes:
        return hashlib.sha256(instance.encode("ascii")).digest()[:16]

    def publish_clip(
        self, session: Session, realm: str, profile: str, instance: str,
        x: float, y: float, z: float, body: str, chat: str | None, chat_hash: bytes | None,
        net: bytes | None = None,
    ) -> float:
        """Replace the caller's clip. Raises AreaFullError without any change."""
        now = self.clock()
        net = session.net if net is None else net
        cx, cz, cy = self.cell_of(x, y, z)
        db = self.db
        db.execute("BEGIN")
        try:
            if self.echo_max_per_cell and db.execute(
                "SELECT count(*) FROM (SELECT 1 FROM clip WHERE realm = ? AND profile = ? AND cx = ? "
                "AND cz = ? AND cy = ? AND expires > ? AND sid != ? LIMIT ?)",
                (realm, profile, cx, cz, cy, now, session.sid, self.echo_max_per_cell),
            ).fetchone()[0] >= self.echo_max_per_cell:
                raise AreaFullError()
            db.execute(
                "UPDATE session SET expires = ?, last_publish = ?, net = ?, last_chat = ? WHERE sid = ?",
                (now + self.retention, now, net,
                 chat_hash if chat_hash is not None else session.last_chat, session.sid),
            )
            db.execute("DELETE FROM clip WHERE sid = ?", (session.sid,))
            self._clip_ids += 1
            expires = now + self.echo_ttl
            db.execute(
                "INSERT INTO clip(realm, profile, cx, cz, cy, sid, clip_id, instance, x, y, z, created, "
                "expires, body, chat) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (realm, profile, cx, cz, cy, session.sid, self._clip_ids, self.instance_key(instance),
                 x, y, z, now, expires, body, chat),
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return expires

    def next_echo(
        self, session: Session, realm: str, profile: str, instance: str,
        x: float, y: float, z: float, net: bytes | None = None,
    ) -> Echo | None:
        """Nearest clip not yet delivered to this observer, from another game instance."""
        now = self.clock()
        net = session.net if net is None else net
        db = self.db
        db.execute("BEGIN")
        try:
            db.execute(
                "UPDATE session SET expires = ?, last_fetch = ?, net = ? WHERE sid = ?",
                (now + self.retention, now, net, session.sid),
            )
            row = db.execute(
                self._echo_sql,
                self._echo_params(realm, profile, x, y, z, now, session.sid, self.instance_key(instance)),
            ).fetchone()
            if row is not None:
                db.execute(
                    "INSERT OR IGNORE INTO delivered(observer, clip_id, expires) VALUES (?, ?, ?)",
                    (session.sid, row[1], now + self.echo_ttl),
                )
                if row[8] is not None:  # its archived copy must not come back later
                    db.execute(
                        "INSERT OR IGNORE INTO delivered(observer, clip_id, expires) VALUES (?, ?, ?)",
                        (session.sid, -row[8], now + ARCHIVE_SEEN),
                    )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if row is None:
            return None
        return Echo(self._ref(session.sid, row[0]), max(0.0, now - row[5]), row[2], row[3], row[4],
                    row[6], row[7] if session.chat else None, row[8])

    def explain_echo(self) -> list[str]:
        params = self._echo_params("r", "p", 0.0, 64.0, 0.0, 0.0, 0, b"")
        return [row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + self._echo_sql, params)]

    def clip_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM clip").fetchone()[0]

    # -- maintenance ------------------------------------------------------

    def purge(self, now: float | None = None, limit: int | None = 2000) -> int:
        """Delete expired rows, oldest first, at most ``limit`` per table."""
        now = self.clock() if now is None else now
        lim = -1 if limit is None else limit
        db = self.db
        db.execute("BEGIN")
        try:
            db.execute(
                "DELETE FROM presence WHERE sid IN "
                "(SELECT sid FROM presence WHERE expires <= ? ORDER BY expires LIMIT ?)",
                (now, lim),
            )
            db.execute(
                "DELETE FROM clip WHERE sid IN "
                "(SELECT sid FROM clip WHERE expires <= ? ORDER BY expires LIMIT ?)",
                (now, lim),
            )
            db.execute(
                "DELETE FROM delivered WHERE (observer, clip_id) IN "
                "(SELECT observer, clip_id FROM delivered WHERE expires <= ? ORDER BY expires LIMIT ?)",
                (now, lim),
            )
            expired = [r[0] for r in db.execute(
                "SELECT sid FROM session WHERE expires <= ? ORDER BY expires LIMIT ?", (now, lim)
            )]
            for sid in expired:
                self._forget_echoes(sid)
            db.executemany("DELETE FROM session WHERE sid = ?", ((sid,) for sid in expired))
            removed = len(expired)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        self._session_rows -= removed
        return removed

    def memory_bytes(self) -> int:
        page_count = self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        return page_count * page_size

    def close(self) -> None:
        self.db.close()
