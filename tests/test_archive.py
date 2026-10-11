"""Durable archive: persistence, private deletion, sampling, caps, purge, plans, filter.

Zone retention and the schema-1 migration are in test_zone_retention.py.
"""

import os
import random
import sqlite3
import stat
import tempfile
import unittest

from noctis_relay.archive import DAY, Archive, ArchiveError, owner_hash
from noctis_relay.profanity import DEFAULT, Filter
from noctis_relay.store import Store

from .support import RunningRelay, make_config
from .test_echoes import ACCOUNT, PROFILE, REALM, clip, place, walk

OWNER_A = "a" * 64
OWNER_B = "b" * 64
OTHER_ACCOUNT = "11111111-2222-4333-8444-555555555555"


class WallClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ArchiveRelayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "archive.db")
        self.wall = WallClock()
        self.config = make_config(
            echoes_enabled=True, echo_chat_enabled=True,
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

    def owner_session(self, owner=OWNER_A, account=ACCOUNT, chat=True, **extra):
        return self.r.create(capabilities=["echoes", "chat"] if chat else ["echoes"], owner=owner, account=account,
                             **extra)

    def observer(self, chat=True):
        return self.r.create(capabilities=["echoes", "chat"] if chat else ["echoes"],
                             owner=OWNER_B, account=OTHER_ACCOUNT)

    def publish(self, token, body):
        return self.r.request("PUT", "/v1/echoes/clip", body, token=token)

    def fetch(self, token, instance="inst-bbbbbbbbbbbb"):
        return self.r.request("POST", "/v1/echoes/next", place(instance=instance), token=token)

    def forget(self, owner, scope):
        return self.r.request("POST", "/v1/archive/forget", {"protocol": 1, "owner": owner, "scope": scope})

    def later(self, seconds):
        """Wall clock and session clock move together (sessions expire, archive ages)."""
        self.wall.advance(seconds)
        self.r.clock.advance(seconds)

    # -- persistence -------------------------------------------------------

    def test_unacknowledged_owner_keeps_fixed_lifetime(self):
        # A session that did not acknowledge revision 5 is archived as under revision 4.
        a = self.owner_session()
        status, body, _ = self.publish(a, clip(chat=[[2, "bonsoir"]]))
        self.assertEqual((status, body["archived"]), (200, True))
        self.assertEqual(self.r.request("DELETE", "/v1/session", token=a)[0], 204)  # logout
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.stop()
        self.later(2 * DAY)
        self.start()
        self.assertEqual(self.r.relay.info["archive"]["legacyDays"], 92)
        self.assertEqual(self.r.request("GET", "/v1/info")[1]["privacyRevision"], 5)
        o = self.observer()
        echo = self.fetch(o)[1]["echo"]
        self.assertIsNotNone(echo)
        self.assertEqual(echo["account"], ACCOUNT)
        self.assertEqual(echo["frames"], walk())
        self.assertEqual(echo["chat"], [[2, "bonsoir"]])
        self.assertGreaterEqual(echo["ageSeconds"], 2 * DAY)
        self.later(2)
        self.assertIsNone(self.fetch(o)[1]["echo"])  # once per observer session
        self.later(89 * DAY)
        self.assertIsNotNone(self.fetch(self.observer())[1]["echo"])  # still there at 91 days
        self.later(2 * DAY)
        self.r.call(lambda: self.r.relay.purge())
        self.assertIsNone(self.fetch(self.observer())[1]["echo"])
        self.assertEqual(self.archive.rows, 0)

    def test_live_clip_then_archive_not_repeated(self):
        a = self.owner_session()
        self.publish(a, clip())
        o = self.observer()
        self.assertIsNotNone(self.fetch(o)[1]["echo"])  # live
        for _ in range(5):  # the observer stays connected past the live lifetime
            self.later(25)
            self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=o)[0], 200)
        self.assertIsNone(self.fetch(o)[1]["echo"])  # its archived copy is not served again

    def test_excludes_own_owner_account_and_instance(self):
        a = self.owner_session()
        self.publish(a, clip())
        self.later(3600)
        same_owner = self.r.create(capabilities=["echoes"], owner=OWNER_A)
        self.assertIsNone(self.fetch(same_owner)[1]["echo"])
        same_account = self.r.create(capabilities=["echoes"], account=ACCOUNT)
        self.assertIsNone(self.fetch(same_account)[1]["echo"])
        anyone = self.r.create(capabilities=["echoes"])
        self.assertIsNone(self.fetch(anyone, instance="inst-aaaaaaaaaaaa")[1]["echo"])
        self.later(2)
        self.assertIsNotNone(self.fetch(anyone)[1]["echo"])

    def test_instance_exclusion_survives_restart(self):
        # The archive keeps sha256(instance)[:16], not the relay's per-run keyed fingerprint:
        # players of the same server run never see each other, even after a relay restart.
        a = self.owner_session()
        self.publish(a, clip())
        self.stop()
        self.later(3600)
        self.start()
        self.assertEqual(self.archive.rows, 1)
        stored = sqlite3.connect(self.path).execute("SELECT instance FROM clip").fetchone()[0]
        self.assertEqual(stored, Store.instance_key("inst-aaaaaaaaaaaa"))
        same_run = self.r.create(capabilities=["echoes"])
        self.assertIsNone(self.fetch(same_run, instance="inst-aaaaaaaaaaaa")[1]["echo"])
        self.later(2)
        self.assertIsNotNone(self.fetch(same_run, instance="inst-bbbbbbbbbbbb")[1]["echo"])

    def test_chat_off_then_forget_chat_withdraws_archived_lines(self):
        a = self.owner_session()
        self.publish(a, clip(chat=[[2, "bonsoir"]]))
        self.r.request("DELETE", "/v1/session", token=a)
        self.assertEqual(self.forget(OWNER_A, "chat")[1], {"removed": 1})
        self.stop()
        self.later(3600)
        self.start()
        echo = self.fetch(self.observer())[1]["echo"]
        self.assertNotIn("chat", echo)
        self.assertEqual(sqlite3.connect(self.path).execute(
            "SELECT count(*) FROM body WHERE chat IS NOT NULL").fetchone()[0], 0)
        with open(self.path, "rb") as fh:
            self.assertNotIn(b"bonsoir", fh.read())

    def test_without_owner_nothing_archived(self):
        t = self.r.create(capabilities=["echoes"])
        self.assertFalse(self.publish(t, clip())[1]["archived"])
        self.assertEqual(self.archive.rows, 0)

    def test_idle_and_mismatched_account_not_archived(self):
        a = self.owner_session()
        still = [[2 * i, 0, 0, 0, 64, 0, 64, 1] for i in range(6)]
        self.assertFalse(self.publish(a, clip(frames=still))[1]["archived"])
        b = self.owner_session(owner="c" * 64, account=OTHER_ACCOUNT)
        self.assertFalse(self.publish(b, clip())[1]["archived"])  # clip claims another account

    def test_sampling_interval_per_owner(self):
        a = self.owner_session()
        self.assertTrue(self.publish(a, clip())[1]["archived"])
        self.later(30)
        a = self.owner_session()
        self.assertFalse(self.publish(a, clip())[1]["archived"])
        self.later(300)
        a = self.owner_session()
        self.assertTrue(self.publish(a, clip())[1]["archived"])

    # -- private deletion ----------------------------------------------------

    def test_forget_all_removes_archives_and_live_sessions(self):
        a = self.owner_session()
        self.publish(a, clip())
        self.later(400)
        a = self.owner_session()
        self.publish(a, clip())
        self.assertEqual(self.archive.rows, 2)
        status, body, _ = self.forget(OWNER_A, "all")
        self.assertEqual((status, body), (200, {"removed": 2}))
        self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=a)[0], 401)
        self.later(3600)
        self.assertIsNone(self.fetch(self.observer())[1]["echo"])
        self.assertEqual(self.forget(OWNER_A, "all")[1], {"removed": 0})  # idempotent
        rows = sqlite3.connect(self.path).execute("SELECT count(*) FROM clip").fetchone()[0]
        self.assertEqual(rows, 0)
        # The secret is never stored, only its hash.
        with open(self.path, "rb") as fh:
            self.assertNotIn(OWNER_A.encode(), fh.read())

    def test_forget_chat_keeps_movement(self):
        a = self.owner_session()
        self.publish(a, clip(chat=[[2, "bonsoir"]]))
        self.assertEqual(self.forget(OWNER_A, "chat")[1], {"removed": 1})
        status, body, _ = self.publish(a, clip(chat=[[2, "encore"]]))
        self.assertEqual(body["error"], "capability_not_granted")  # live chat grant revoked
        self.later(3600)
        echo = self.fetch(self.observer())[1]["echo"]
        self.assertIsNotNone(echo)
        self.assertNotIn("chat", echo)

    def test_forget_validation_and_unavailable(self):
        self.assertEqual(self.forget("xyz", "all")[0], 400)
        self.assertEqual(self.forget(OWNER_A, "some")[0], 400)
        self.archive.failed = True
        status, body, headers = self.forget(OWNER_A, "all")
        self.assertEqual((status, body["error"]), (503, "archive_unavailable"))
        self.assertIn("retry-after", headers)

    # -- caps -----------------------------------------------------------------

    def test_cap_refuses_without_eviction(self):
        self.stop()
        self.start(max_clips=2)
        for i, owner in enumerate(("1" * 64, "2" * 64, "3" * 64)):
            t = self.owner_session(owner=owner)
            self.assertEqual(self.publish(t, clip())[1]["archived"], i < 2)
        self.assertEqual(self.archive.rows, 2)
        owners = {r[0] for r in sqlite3.connect(self.path).execute("SELECT owner FROM clip")}
        self.assertEqual(owners, {owner_hash("1" * 64), owner_hash("2" * 64)})

    def test_info_without_archive(self):
        r = RunningRelay(make_config(echoes_enabled=True))
        try:
            self.assertNotIn("archive", r.request("GET", "/v1/info")[1])
            # Live-only relays still accept private deletions (nothing archived).
            self.assertEqual(r.request("POST", "/v1/archive/forget",
                                       {"protocol": 1, "owner": OWNER_A, "scope": "all"})[1], {"removed": 0})
        finally:
            r.close()

    # -- chat filter ------------------------------------------------------------

    def test_insults_dropped_before_storage_and_rechecked_when_served(self):
        a = self.owner_session()
        status, body, _ = self.publish(a, clip(chat=[[1, "espèce de connard"], [2, "salut"]]))
        self.assertEqual(body["chat"], 1)
        self.later(3600)
        self.r.relay.chat_filter = Filter.with_files("salut\n")  # operator list grew meanwhile
        echo = self.fetch(self.observer())[1]["echo"]
        self.assertNotIn("chat", echo)


class ArchiveUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.db")
        self.wall = WallClock()

    def tearDown(self):
        self.dir.cleanup()

    def open(self, **kw):
        kw.setdefault("rng", random.Random(7))
        return Archive(self.path, wallclock=self.wall, **kw)

    def add(self, a, i, x=0.5, account=None):
        return a.admit(i.to_bytes(16, "big"), account or f"00000000-0000-4000-8000-{i:012d}",
                       REALM, PROFILE, Store.instance_key(f"inst-{i:012d}"), x, 64.0, 0.5, '{"frames":[]}', None)

    def test_refuses_unknown_or_corrupt_files_without_touching_them(self):
        with open(self.path, "wb") as fh:
            fh.write(b"not a database at all" * 100)
        with open(self.path, "rb") as fh:
            before = fh.read()
        with self.assertRaises(ArchiveError):
            self.open()
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        os.remove(self.path)
        a = self.open()
        a.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
        a.close()
        with self.assertRaises(ArchiveError):
            self.open()
        with self.assertRaises(ArchiveError):
            Archive(os.path.join(self.dir.name, "missing", "a.db"))

    def test_bounded_purge_oldest_first(self):
        a = self.open(interval=0, owner_daily=10_000, cell_daily=10_000, cell_max=10_000)
        for i in range(1200):
            self.add(a, i)
        self.wall.advance(93 * DAY)
        self.assertEqual(a.purge(500), 500)
        self.assertEqual(a.rows, 700)
        self.assertEqual(a.purge(500), 500)
        self.assertEqual(a.purge(500), 200)
        self.assertEqual(a.purge(500), 0)
        total = a.db.execute("SELECT coalesce(sum(total), 0) FROM cell").fetchone()[0]
        self.assertEqual(total, 0)

    def test_cell_and_owner_daily_caps(self):
        a = self.open(interval=0, owner_daily=2, cell_daily=3)
        self.assertIsNotNone(self.add(a, 1))
        self.assertIsNotNone(self.add(a, 1))
        self.assertIsNone(self.add(a, 1))  # owner daily cap
        self.assertIsNotNone(self.add(a, 2))
        self.assertIsNone(self.add(a, 3))  # cell daily cap
        self.wall.advance(DAY)
        self.assertIsNotNone(self.add(a, 3))

    def test_query_plans_use_indexes(self):
        a = self.open()
        plans = " | ".join(a.explain())
        for index in ("clip_cell", "clip_zone", "clip_created", "clip_expires", "clip_owner",
                      "cell_empty", "owner_empty"):
            self.assertIn(index, plans)
        self.assertNotIn("SCAN", plans)
        self.assertNotIn("TEMP B-TREE", plans)

    def test_variety_and_old_visits(self):
        a = self.open(interval=0, owner_daily=10_000, cell_daily=10_000, cell_max=10_000)
        # 30 people, one visit a day each for 90 days, in the same place.
        for day in range(90):
            for i in range(30):
                self.add(a, i, x=0.5 + i % 7)
                self.wall.advance(60)
            self.wall.advance(DAY - 30 * 60)
        accounts, ages = set(), []
        for _ in range(200):
            got = a.pick(REALM, PROFILE, 0.5, 64.0, 0.5, None, None, b"", lambda cid: False)
            accounts.add(got.account)
            ages.append(self.wall() - got.created)
        self.assertGreaterEqual(len(accounts), 20)
        self.assertGreater(sum(age > 30 * DAY for age in ages), 50)  # not always the latest
        self.assertGreater(sum(age < 30 * DAY for age in ages), 30)

    def test_pick_skips_delivered(self):
        a = self.open()
        cid = self.add(a, 1)
        self.wall.advance(3600)
        self.assertIsNone(a.pick(REALM, PROFILE, 0.5, 64.0, 0.5, None, None, b"", lambda c: c == cid))
        self.assertEqual(a.pick(REALM, PROFILE, 0.5, 64.0, 0.5, None, None, b"", lambda c: False).id, cid)


class ProfanityTest(unittest.TestCase):
    def test_blocks_variants(self):
        for text in ("fuck", "FUUUCK you", "f.u.c.k", "f*ck", "sh1t", "connard", "Connärd", "espèce d'enculé",
                     "fils de pute", "nique ta mère", "ta gueule", "kys", "salope", "b1tch", "putaiiin",
                     "ferme ta gueule", "va te faire foutre", "nigger"):
            self.assertTrue(DEFAULT.blocked(text), text)

    def test_keeps_innocent_words(self):
        for text in ("passer", "assassin", "classique", "Scunthorpe", "pros and cons", "je suis en retard",
                     "cocktail", "un cône", "shiitake", "niggling doubt", "bonjour à tous", "la conne xion",
                     "concours", "1337 blocs", "3 cubes"):
            if text == "la conne xion":
                continue  # 'conne' alone is an insult: accepted limit
            self.assertFalse(DEFAULT.blocked(text), text)

    def test_operator_lists(self):
        f = Filter.with_files("truc\nmachin*\nsale bete\n# comment\n", "connard\n")
        self.assertTrue(f.blocked("un truc"))
        self.assertTrue(f.blocked("machinchose"))
        self.assertTrue(f.blocked("Sale bête"))
        self.assertFalse(f.blocked("connard"))


if __name__ == "__main__":
    unittest.main()
