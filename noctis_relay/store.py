"""In-memory SQLite store for sessions and optional presence.

Nothing is written to disk: the database lives in process memory and is
lost on restart, which is the intended retention policy. All access happens
from the single event-loop thread, so one connection is enough and there is
no writer contention.
"""

from __future__ import annotations

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
    net BLOB NOT NULL DEFAULT x''
);
CREATE INDEX session_expires ON session(expires);
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
"""


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


@dataclass(frozen=True)
class Neighbour:
    ref: str
    x: float
    y: float
    z: float
    distance: float


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

    # -- sessions ---------------------------------------------------------

    def session_count(self) -> int:
        return self._session_rows

    def network_sessions(self, net: bytes) -> int:
        """Live sessions attributed to ``net`` (index range, no table scan)."""
        return self.db.execute(
            "SELECT count(*) FROM session WHERE net = ? AND expires > ?", (net, self.clock())
        ).fetchone()[0]

    def create_session(self, presence: bool = False, net: bytes = b"") -> tuple[str, Session]:
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
            "INSERT INTO session(token_hash, expires, presence, net) VALUES (?, ?, ?, ?)",
            (hash_token(token), expires, 1 if presence else 0, net),
        )
        self._session_rows += 1
        return token, Session(cur.lastrowid, expires, presence, -1e300, net)

    def find_session(self, token: str) -> Session | None:
        row = self.db.execute(
            "SELECT sid, expires, presence, last_presence, net FROM session WHERE token_hash = ?",
            (hash_token(token),),
        ).fetchone()
        if row is None or row[1] <= self.clock():
            return None
        return Session(row[0], row[1], bool(row[2]), row[3], row[4])

    def refresh_session(self, session: Session, net: bytes | None = None) -> Session | None:
        """Extend a live session and attribute it to ``net`` (if given)."""
        now = self.clock()
        expires = now + self.retention
        net = session.net if net is None else net
        row = self.db.execute(
            "UPDATE session SET expires = ?, net = ? WHERE sid = ? AND expires > ? "
            "RETURNING sid, presence, last_presence",
            (expires, net, session.sid, now),
        ).fetchone()
        if row is None:
            return None
        return Session(row[0], expires, bool(row[1]), row[2], net)

    def delete_session(self, token: str) -> bool:
        db = self.db
        db.execute("BEGIN")
        try:
            row = db.execute(
                "DELETE FROM session WHERE token_hash = ? RETURNING sid", (hash_token(token),)
            ).fetchone()
            if row is not None:
                db.execute("DELETE FROM presence WHERE sid = ?", (row[0],))
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
            removed = db.execute(
                "DELETE FROM session WHERE sid IN "
                "(SELECT sid FROM session WHERE expires <= ? ORDER BY expires LIMIT ?)",
                (now, lim),
            ).rowcount
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
