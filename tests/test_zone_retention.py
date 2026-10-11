"""Zone retention (privacy revision 5), notice acknowledgement and the schema-1 migration."""

import importlib.util
import json
import math
import os
import random
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from noctis_relay.archive import DAY, REPLACE_BATCH, Archive, ArchiveError, owner_hash
from noctis_relay.config import ConfigError
from noctis_relay.store import Store

from .support import RunningRelay, make_config
from .test_archive import OTHER_ACCOUNT, OWNER_A, OWNER_B, WallClock
from .test_echoes import ACCOUNT, PROFILE, REALM, clip, place

spec = importlib.util.spec_from_file_location("seed_previews", Path(__file__).parents[1] / "tools/seed_previews.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

OWNER_C = "c" * 64
THIRD_ACCOUNT = "22222222-3333-4444-8555-666666666666"
BODY = '{"frames":[]}'
# Zones (32 x 16 x 32 cells) at y 64: A busy, B and Q quiet.
A, B, Q = 0.5, 100.5, 1000.5

# Schema 1 exactly as relay 0.4.0 created it.
V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE clip (
    id INTEGER PRIMARY KEY, realm TEXT NOT NULL, profile TEXT NOT NULL,
    cx INTEGER NOT NULL, cz INTEGER NOT NULL, cy INTEGER NOT NULL, created INTEGER NOT NULL,
    owner BLOB NOT NULL, account TEXT NOT NULL, instance BLOB NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL, size INTEGER NOT NULL, shuffle INTEGER NOT NULL
);
CREATE INDEX clip_cell ON clip(realm, profile, cx, cz, cy, shuffle);
CREATE INDEX clip_owner ON clip(owner);
CREATE INDEX clip_created ON clip(created);
CREATE TABLE body (id INTEGER PRIMARY KEY, body BLOB NOT NULL, chat TEXT);
CREATE TABLE cell (
    realm TEXT NOT NULL, profile TEXT NOT NULL,
    cx INTEGER NOT NULL, cz INTEGER NOT NULL, cy INTEGER NOT NULL,
    total INTEGER NOT NULL, day INTEGER NOT NULL, today INTEGER NOT NULL,
    PRIMARY KEY (realm, profile, cx, cz, cy)
) WITHOUT ROWID;
CREATE TABLE owner (
    owner BLOB PRIMARY KEY,
    last INTEGER NOT NULL, day INTEGER NOT NULL, today INTEGER NOT NULL, total INTEGER NOT NULL
) WITHOUT ROWID;
"""


def build_v1(path: str, rows: list, extra: str = "") -> None:
    """A schema-1 archive holding ``rows`` = (realm, profile, x, y, z, created, owner, account, body, chat)."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("BEGIN;" + V1_SCHEMA + extra + "INSERT INTO meta VALUES ('schema', '1'), ('created', '0');COMMIT;")
    for i, (realm, profile, x, y, z, created, owner, account, body, chat) in enumerate(rows, 1):
        zone = (realm, profile, math.floor(x / 32), math.floor(z / 32), math.floor(y / 16))
        packed = zlib.compress(body.encode())
        db.execute("INSERT INTO clip VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (i, *zone, created, owner, account, Store.instance_key(f"legacy-instance-{i:04d}"),
                    x, y, z, len(packed) + 160, random.Random(i).getrandbits(31)))
        db.execute("INSERT INTO body VALUES (?, ?, ?)", (i, packed, chat))
        day = created // DAY
        db.execute("INSERT INTO cell VALUES (?, ?, ?, ?, ?, 1, ?, 1) ON CONFLICT DO UPDATE SET total = total + 1",
                   (*zone, day))
        db.execute("INSERT INTO owner VALUES (?, ?, ?, 1, 1) ON CONFLICT DO UPDATE SET total = total + 1",
                   (owner, created, day))
    db.close()


class ZoneUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.db")
        self.wall = WallClock()
        self.archives = []

    def tearDown(self):
        for a in self.archives:
            a.close()
        self.dir.cleanup()

    def open(self, **kw):
        for key, value in (("interval", 0), ("owner_daily", 10**6), ("cell_daily", 10**6), ("min_age", 0)):
            kw.setdefault(key, value)
        kw.setdefault("rng", random.Random(7))
        a = Archive(self.path, wallclock=self.wall, **kw)
        self.archives.append(a)
        return a

    def add(self, a, i, x=A, fixed=False, chat=None):
        """One clip of owner ``i`` (zone retention unless ``fixed``)."""
        return a.admit(i.to_bytes(16, "big"), f"00000000-0000-4000-8000-{i:012d}", REALM, PROFILE,
                       Store.instance_key(f"inst-{i:012d}"), x, 64.0, 0.5, BODY, chat, fixed=fixed)

    def zone(self, a, x):
        cx, cz, cy = a.cell_of(x, 64.0, 0.5)
        ids = [r[0] for r in a.db.execute(
            "SELECT id FROM clip WHERE cx = ? AND cz = ? AND cy = ? ORDER BY id", (cx, cz, cy))]
        total = a.db.execute("SELECT total FROM cell WHERE cx = ? AND cz = ? AND cy = ?", (cx, cz, cy)).fetchone()
        self.assertEqual(len(ids), total[0] if total else 0)  # the quota counter stays exact
        return ids

    def pick(self, a, x=A, seen=()):
        return a.pick(REALM, PROFILE, x, 64.0, 0.5, exclude_owner=None, exclude_account=None,
                      exclude_instance=b"\0" * 16, delivered=lambda cid: cid in seen)

    def test_sparse_zone_keeps_old_clip_through_maintenance_and_restart(self):
        a = self.open()
        cid = self.add(a, 1)
        self.assertIsNone(a.db.execute("SELECT expires FROM clip").fetchone()[0])
        for _ in range(4):  # five years of maintenance
            self.wall.advance(456 * DAY)
            self.assertEqual(a.purge(1000), 0)
        a.close()
        a = self.open()
        found = self.pick(a)
        self.assertEqual(found.id, cid)
        self.assertGreater(a.now() - found.created, 92 * DAY)

    def test_full_zone_replaces_its_own_oldest_from_the_floor(self):
        a = self.open(cell_max=3)
        busy = []
        for i in range(3):
            busy.append(self.add(a, i))
            self.wall.advance(DAY)
        quiet = self.add(a, 10, B)
        self.wall.advance(89 * DAY)  # the oldest clip of A is exactly 92 days old
        new = self.add(a, 3)
        self.assertIsNotNone(new)
        self.assertEqual(self.zone(a, A), busy[1:] + [new])
        self.assertEqual(self.zone(a, B), [quiet])
        self.assertEqual(a.rows, 4)
        self.assertIsNone(self.add(a, 4))  # the next oldest is only 91 days old
        self.assertEqual(self.zone(a, A), busy[1:] + [new])
        self.wall.advance(DAY)
        self.assertIsNotNone(self.add(a, 5))
        self.assertEqual(self.zone(a, A)[:2], [busy[2], new])
        self.assertEqual(self.zone(a, B), [quiet])

    def test_full_zone_of_young_clips_refuses_without_deleting(self):
        a = self.open(cell_max=3)
        busy = [self.add(a, i) for i in range(3)]
        self.wall.advance(92 * DAY - 1)
        self.assertIsNone(self.add(a, 3))
        self.assertEqual(self.zone(a, A), busy)
        self.assertIsNotNone(self.add(a, 4, B))  # other zones still admit

    def test_quiet_zone_below_quota_is_never_thinned_when_the_archive_is_full(self):
        a = self.open(cell_max=2, max_clips=3)
        old = self.add(a, 0, Q)
        busy = [self.add(a, 1), self.add(a, 2)]
        self.wall.advance(400 * DAY)
        # Q is below its quota: the relay-wide cap refuses, its old visit stays.
        self.assertIsNone(self.add(a, 3, Q))
        self.assertEqual(self.zone(a, Q), [old])
        self.assertIsNone(self.add(a, 4, B))  # a new zone is refused too
        self.assertEqual(self.zone(a, B), [])
        # A is full: it replaces its own oldest and stays within the cap.
        new = self.add(a, 5)
        self.assertEqual(self.zone(a, A), [busy[1], new])
        self.assertEqual(self.zone(a, Q), [old])
        self.assertEqual(a.rows, 3)
        self.assertEqual(self.pick(a, Q).id, old)

    def test_byte_cap_refuses_rather_than_deleting(self):
        a = self.open(cell_max=2)
        old = self.add(a, 0, Q)
        busy = [self.add(a, 1), self.add(a, 2)]
        a.close()
        a = self.open(cell_max=2, max_bytes=1)  # far over: no same-zone batch can free enough
        self.wall.advance(400 * DAY)
        self.assertIsNone(self.add(a, 3, Q))
        self.assertIsNone(self.add(a, 4))
        self.assertEqual((self.zone(a, Q), self.zone(a, A)), ([old], busy))

    def test_lowered_quota_converges_on_old_clips_only(self):
        a = self.open(cell_max=100)
        for i in range(40):
            self.add(a, i)
        quiet = self.add(a, 99, B)
        a.close()
        a = self.open(cell_max=10)
        self.wall.advance(92 * DAY)
        young = []
        for expected in (40 - REPLACE_BATCH + 1, 10, 10):
            young.append(self.add(a, 100 + len(young)))
            self.assertIsNotNone(young[-1])
            self.assertEqual(len(self.zone(a, A)), expected)
        self.assertEqual(self.zone(a, A)[-3:], young)
        self.assertEqual(self.zone(a, B), [quiet])

    def test_abuse_budgets_hold_even_when_a_victim_exists(self):
        a = self.open(cell_max=3, cell_daily=2, interval=300, owner_daily=2)
        old = [self.add(a, 0), self.add(a, 1)]
        self.wall.advance(DAY)
        old.append(self.add(a, 2))
        self.wall.advance(92 * DAY)
        self.assertIsNotNone(self.add(a, 10))
        self.assertIsNone(self.add(a, 10))  # same owner within the interval
        self.assertIsNotNone(self.add(a, 11))
        self.assertIsNone(self.add(a, 12))  # cell/day budget
        self.assertEqual(self.zone(a, A)[0], old[2])  # nothing replaced by refusals
        self.wall.advance(DAY)
        self.assertIsNotNone(self.add(a, 10))
        self.wall.advance(300)
        self.assertIsNotNone(self.add(a, 10, B))
        self.wall.advance(300)
        self.assertIsNone(self.add(a, 10, B))  # owner/day budget (2) still applies...
        self.wall.advance(DAY)
        self.assertIsNotNone(self.add(a, 10, B))  # ...until the next UTC day

    def test_fixed_lifetime_hidden_when_due_and_purged(self):
        a = self.open()
        legacy = self.add(a, 1, fixed=True)
        kept = self.add(a, 2, B)
        self.assertEqual(a.db.execute("SELECT expires - created FROM clip WHERE id = ?", (legacy,)).fetchone()[0],
                         92 * DAY)
        self.wall.advance(92 * DAY)
        self.assertIsNone(self.pick(a))  # due but not purged yet
        self.assertEqual(a.rows, 2)
        self.assertEqual(a.purge(), 1)
        self.assertEqual(self.pick(a, B).id, kept)

    def test_optional_maximum_age(self):
        with self.assertRaises(ArchiveError):
            self.open(max_days=91)
        with self.assertRaises(ConfigError):
            make_config(archive_max_days=91)
        make_config(archive_max_days=0)
        a = self.open(max_days=100)
        cid = self.add(a, 1)
        self.wall.advance(100 * DAY)
        self.assertEqual(a.purge(), 0)
        self.assertEqual(self.pick(a).id, cid)
        self.wall.advance(1)
        self.assertIsNone(self.pick(a))
        self.assertEqual(a.purge(), 1)

    def test_ids_never_reused_while_running(self):
        a = self.open()
        first = self.add(a, 1)
        self.assertEqual(a.forget((1).to_bytes(16, "big"), False), 1)
        second = self.add(a, 2)
        self.assertGreater(second, first)
        self.assertEqual(a.forget((2).to_bytes(16, "big"), False), 1)
        a.close()
        a = self.open()  # delivery records start empty at boot: the file maximum suffices
        self.assertGreater(self.add(a, 3), 0)

    def test_migration_keeps_legacy_lifetime_and_demo_clips(self):
        t0 = int(self.wall())
        key = b"k" * 32
        demos = []
        for i, scene in enumerate(seed.SCENES):
            body = seed.clip(scene, .5, 80, .5, seed.DEFAULT_PROFILE, "preview_fictional_instance")
            demos.append((seed.REALM, seed.DEFAULT_PROFILE, .5 + i * .1, 80.0, .5, t0 - 10 * DAY,
                          owner_hash(seed.owner(key, scene)), seed.account(scene),
                          json.dumps({k: body[k] for k in ("account", "frames", "actions")}), None))
        real = (REALM, PROFILE, 500.5, 64.0, .5, t0 - 80 * DAY, owner_hash(OWNER_C), THIRD_ACCOUNT,
                BODY, '[[2,"bonsoir"]]')
        build_v1(self.path, demos + [real])

        a = self.open()
        meta = dict(a.db.execute("SELECT key, value FROM meta"))
        self.assertEqual((meta["schema"], meta["legacy_days"]), ("2", "92"))
        self.assertIn("migrated_from_1", meta)
        self.assertEqual(a.db.execute("SELECT count(*) FROM clip WHERE expires = created + ?",
                                      (92 * DAY,)).fetchone()[0], 9)
        self.assertNotIn("SCAN", " ".join(a.explain()))

        def demo_accounts(arch):
            seen, accounts = set(), set()
            while (found := arch.pick(seed.REALM, seed.DEFAULT_PROFILE, .5, 80, .5, None, None, b"\0" * 16,
                                      lambda cid: cid in seen)) is not None:
                seen.add(found.id)
                accounts.add(found.account)
            return accounts

        self.assertEqual(demo_accounts(a), {seed.account(scene) for scene in seed.SCENES})
        # New revision-5 clip in a quiet zone, id above the migrated ones.
        new = self.add(a, 1, Q)
        self.assertGreater(new, 9)
        self.assertEqual(a.forget(owner_hash(OWNER_C), True), 1)  # private chat removal on a legacy row

        self.wall.advance(13 * DAY)  # the real legacy clip is 93 days old
        self.assertIsNone(a.pick(REALM, PROFILE, 500.5, 64, .5, None, None, b"\0" * 16, lambda cid: False))
        self.assertEqual(a.purge(), 1)
        self.assertEqual(a.rows, 9)
        a.close()

        a = self.open()  # reopening schema 2 does not migrate again
        self.assertEqual(dict(a.db.execute("SELECT key, value FROM meta"))["migrated_from_1"], meta["migrated_from_1"])
        self.assertEqual(len(demo_accounts(a)), 8)
        self.wall.advance(80 * DAY)  # demos 103 days old: their notice promised 92
        self.assertEqual(a.purge(), 8)
        found = self.pick(a, Q)
        self.assertEqual(found.id, new)
        self.assertGreater(a.now() - found.created, 92 * DAY)
        self.assertEqual(a.forget((1).to_bytes(16, "big"), False), 1)
        self.assertEqual(a.rows, 0)

    def test_migration_uses_the_configured_legacy_lifetime(self):
        t0 = int(self.wall())
        build_v1(self.path, [(REALM, PROFILE, A, 64.0, .5, t0, b"o" * 16, ACCOUNT, BODY, None)])
        a = self.open(legacy_days=30)
        self.assertEqual(a.db.execute("SELECT expires FROM clip").fetchone()[0], t0 + 30 * DAY)

    def test_failed_migration_leaves_schema_1_untouched(self):
        t0 = int(self.wall())
        build_v1(self.path, [(REALM, PROFILE, A, 64.0, .5, t0, b"o" * 16, ACCOUNT, BODY, None)],
                 extra="CREATE INDEX clip_zone ON clip(created);")
        with self.assertRaises(ArchiveError):
            self.open()
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0], "1")
            self.assertNotIn("expires", [r[1] for r in db.execute("PRAGMA table_info(clip)")])
            self.assertEqual(db.execute("SELECT count(*) FROM clip").fetchone()[0], 1)
        finally:
            db.close()


class RelayFixture:
    """A relay with an archive on disk; wall clock and session clock move together."""

    gear = True

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "archive.db")
        self.wall = WallClock()
        self.config = make_config(
            echoes_enabled=True, echo_chat_enabled=True, echo_gear_enabled=self.gear,
            echo_publish_interval=1.0, echo_fetch_interval=1.0,
            global_echo_rate=0.0, archive_path=self.path, archive_rate=0.0,
        )
        self.start()

    def start(self, **archive):
        self.archive = Archive(self.path, wallclock=self.wall, min_age=self.config.echo_ttl, **archive)
        self.r = RunningRelay(self.config, archive=self.archive)

    def stop(self):
        self.r.close()
        self.archive.close()

    def tearDown(self):
        self.stop()
        self.dir.cleanup()

    def owner_session(self, owner=OWNER_A, account=ACCOUNT, **extra):
        return self.r.create(capabilities=["echoes", "chat"], owner=owner, account=account, **extra)

    def publish(self, token, body):
        return self.r.request("PUT", "/v1/echoes/clip", body, token=token)

    def fetch(self, token, instance="inst-bbbbbbbbbbbb"):
        return self.r.request("POST", "/v1/echoes/next", place(instance=instance), token=token)

    def later(self, seconds, *keep):
        """Advance both clocks; sessions in ``keep`` are refreshed on the way."""
        while seconds > 0:
            step = min(seconds, 20)
            self.wall.advance(step)
            self.r.clock.advance(step)
            seconds -= step
            for token in keep:
                self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=token)[0], 200)


class ZoneRelayTest(RelayFixture, unittest.TestCase):
    def expires(self):
        return [r[0] for r in sqlite3.connect(self.path).execute("SELECT expires FROM clip ORDER BY id")]

    def test_info_distinguishes_floor_from_lifetime(self):
        info = self.r.request("GET", "/v1/info")[1]
        self.assertEqual(info["privacyRevision"], 5)
        self.assertEqual(info["archive"], {
            "ageFloorDays": 92, "maxAgeDays": None, "zoneQuota": 4096, "zone": {"width": 32, "height": 16},
            "legacyDays": 92, "intervalSeconds": 300, "ownerDaily": 48, "cellDaily": 24,
        })

    def test_acknowledged_sparse_clip_served_after_years_and_restart(self):
        a = self.owner_session(privacyRevision=5)
        self.assertTrue(self.publish(a, clip(chat=[[2, "bonsoir"]]))[1]["archived"])
        self.assertEqual(self.r.request("DELETE", "/v1/session", token=a)[0], 204)
        self.assertEqual(self.expires(), [None])
        self.stop()
        self.later(3 * 365 * DAY)
        self.start()
        self.r.call(lambda: self.r.relay.purge())
        o = self.r.create(capabilities=["echoes", "chat"])
        echo = self.fetch(o)[1]["echo"]
        self.assertEqual((echo["account"], echo["chat"]), (ACCOUNT, [[2, "bonsoir"]]))
        self.assertGreaterEqual(echo["ageSeconds"], 3 * 365 * DAY)
        self.assertEqual(self.r.request("POST", "/v1/archive/forget",
                                        {"protocol": 1, "owner": OWNER_A, "scope": "all"})[1], {"removed": 1})
        self.assertEqual(self.archive.rows, 0)

    def test_privacy_revision_acknowledgement(self):
        for bad in (6, 0, -1, True, "5", 5.0, None, [5]):
            status, body, _ = self.r.request("POST", "/v1/sessions", {
                "protocol": 1, "capabilities": ["echoes"], "owner": OWNER_A, "privacyRevision": bad})
            self.assertEqual((status, body["error"]), (400, "invalid_privacy_revision"), bad)
        owners = [("d" * 64, {}), ("e" * 64, {"privacyRevision": 4}), ("f" * 64, {"privacyRevision": 5})]
        for i, (owner, extra) in enumerate(owners):
            token = self.owner_session(owner=owner, **extra)
            body = clip(instance=f"inst-{i:012d}")
            self.assertTrue(self.publish(token, body)[1]["archived"])
        now = int(self.wall())
        self.assertEqual(self.expires(), [now + 92 * DAY, now + 92 * DAY, None])

    def test_forgotten_clip_id_does_not_hide_a_new_clip(self):
        o = self.r.create(capabilities=["echoes"])
        a = self.owner_session(privacyRevision=5)
        self.assertTrue(self.publish(a, clip())[1]["archived"])
        self.assertEqual(self.fetch(o)[1]["echo"]["account"], ACCOUNT)  # live, its archive copy marked seen
        self.assertEqual(self.r.request("POST", "/v1/archive/forget",
                                        {"protocol": 1, "owner": OWNER_A, "scope": "all"})[1], {"removed": 1})
        self.later(30, o)
        b = self.owner_session(owner=OWNER_C, account=THIRD_ACCOUNT, privacyRevision=5)
        self.assertTrue(self.publish(b, clip(instance="inst-cccccccccccc", account=THIRD_ACCOUNT))[1]["archived"])
        self.later(self.config.echo_ttl + 5, o)  # live copy gone, archived copy servable
        echo = self.fetch(o)[1]["echo"]
        self.assertIsNotNone(echo)
        self.assertEqual(echo["account"], THIRD_ACCOUNT)


if __name__ == "__main__":
    unittest.main()
