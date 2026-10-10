import random
import unittest

from noctis_relay.store import Store

from .support import RunningRelay, make_config

REALM = "astralyth:noctis"
PROFILE = "gen1-a1b2c3d4"


def pos(x, y, z, instance="inst-aaaaaaaaaaaa", realm=REALM, profile=PROFILE):
    return {"realm": realm, "profile": profile, "instance": instance, "x": x, "y": y, "z": z}


class PresenceApiTest(unittest.TestCase):
    def setUp(self):
        self.r = RunningRelay(make_config(presence_enabled=True, presence_min_interval=0.0))

    def tearDown(self):
        self.r.close()

    def put(self, token, body):
        return self.r.request("PUT", "/v1/presence", body, token=token)

    def test_info_advertises_capability(self):
        _, body, _ = self.r.request("GET", "/v1/info")
        self.assertEqual(body["capabilities"], ["sessions", "presence"])
        self.assertEqual(body["presence"]["radius"], 48)

    def test_presence_requires_explicit_opt_in(self):
        plain = self.r.create()
        status, body, _ = self.put(plain, pos(0, 64, 0))
        self.assertEqual((status, body["error"]), (403, "capability_not_granted"))
        status, body, _ = self.r.request("POST", "/v1/sessions", {"protocol": 1, "capabilities": ["presence"]})
        self.assertEqual((status, body["capabilities"]), (201, ["presence"]))
        self.assertEqual(self.put(body["token"], pos(0, 64, 0))[0], 200)
        self.assertEqual(self.put("f" * 64, pos(0, 64, 0))[0], 401)

    def test_neighbours_filtering(self):
        a = self.r.create(capabilities=["presence"])
        b = self.r.create(capabilities=["presence"])
        c = self.r.create(capabilities=["presence"])
        d = self.r.create(capabilities=["presence"])
        e = self.r.create(capabilities=["presence"])
        self.put(b, pos(-10.5, 64, -3, instance="inst-bbbbbbbbbbbb"))          # 10 blocks away
        self.put(c, pos(0, 40, 0, instance="inst-cccccccccccc"))               # cave below: dy 24
        self.put(d, pos(5, 64, 5, instance="inst-dddddddddddd", profile="gen1-other0000"))  # other profile
        self.put(e, pos(2, 64, 2, instance="inst-aaaaaaaaaaaa"))               # same save instance as a
        status, body, _ = self.put(a, pos(0.5, 64, 0, instance="inst-aaaaaaaaaaaa"))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["nearby"]), 1)
        near = body["nearby"][0]
        self.assertEqual((near["x"], near["y"], near["z"]), (-10.5, 64.0, -3.0))
        self.assertRegex(near["ref"], r"[0-9a-f]{16}")
        # Refs are per observer: c sees b under a different handle.
        status, body, _ = self.put(c, pos(-10, 60, -3, instance="inst-cccccccccccc"))
        refs = {n["ref"] for n in body["nearby"]}
        self.assertNotIn(near["ref"], refs)

    def test_presence_expires_and_is_cleared(self):
        a = self.r.create(capabilities=["presence"])
        b = self.r.create(capabilities=["presence"])
        self.put(b, pos(1, 64, 1, instance="inst-bbbbbbbbbbbb"))
        self.assertEqual(len(self.put(a, pos(0, 64, 0))[1]["nearby"]), 1)
        self.assertEqual(self.r.request("DELETE", "/v1/presence", token=b)[0], 204)
        self.assertEqual(self.put(a, pos(0, 64, 0))[1]["nearby"], [])
        self.put(b, pos(1, 64, 1, instance="inst-bbbbbbbbbbbb"))
        # A session heartbeat does not keep a stale position alive.
        self.r.clock.advance(20)
        self.r.request("PUT", "/v1/session", {}, token=b)
        self.put(a, pos(0, 64, 0))
        self.r.clock.advance(11)
        self.assertEqual(self.put(a, pos(0, 64, 0))[1]["nearby"], [])
        # Deleting the session removes its presence row.
        self.put(b, pos(1, 64, 1, instance="inst-bbbbbbbbbbbb"))
        self.r.request("DELETE", "/v1/session", token=b)
        rows = self.r.call(lambda: self.r.relay.store.db.execute("SELECT count(*) FROM presence").fetchone()[0])
        self.assertEqual(rows, 1)

    def test_bad_presence_bodies(self):
        a = self.r.create(capabilities=["presence"])
        bad = [
            {**pos(0, 64, 0), "uuid": "x"},
            {**pos(0, 64, 0), "x": True},
            {**pos(0, 64, 0), "x": "1"},
            {**pos(0, 64, 0), "x": 3e7 + 1},
            {**pos(0, 64, 0), "y": 5000},
            {**pos(0, 64, 0), "y": None},
            pos(0, 64, 0, realm="Noctis"),
            pos(0, 64, 0, realm="a:b\n"),
            pos(0, 64, 0, profile="short"),
            pos(0, 64, 0, profile="x" * 65),
            pos(0, 64, 0, instance="tooshort"),
            pos(0, 64, 0, instance="has space in it!!"),
        ]
        for body in bad:
            status, resp, _ = self.put(a, body)
            self.assertEqual(status, 400, (body, resp))
        status, resp, _ = self.r.request(
            "PUT", "/v1/presence", token=a, headers={"Content-Type": "application/json"},
            raw_body=b'{"realm":"a:b","profile":"gen1-a1b2c3d4","instance":"inst-aaaaaaaaaaaa","x":NaN,"y":1,"z":1}',
        )
        self.assertEqual((status, resp["error"]), (400, "invalid_json"))
        status, resp, _ = self.r.request(
            "PUT", "/v1/presence", token=a, headers={"Content-Type": "application/json"},
            raw_body=b'{"realm":"a:b","profile":"gen1-a1b2c3d4","instance":"inst-aaaaaaaaaaaa","x":1e999,"y":1,"z":1}',
        )
        self.assertEqual((status, resp["error"]), (400, "invalid_x"))

    def test_presence_rate(self):
        r = RunningRelay(make_config(presence_enabled=True, presence_min_interval=1.0))
        try:
            a = r.create(capabilities=["presence"])
            self.assertEqual(r.request("PUT", "/v1/presence", pos(0, 64, 0), token=a)[0], 200)
            self.assertEqual(r.request("PUT", "/v1/presence", pos(0, 64, 0), token=a)[0], 429)
            r.clock.advance(1.0)
            self.assertEqual(r.request("PUT", "/v1/presence", pos(0, 64, 0), token=a)[0], 200)
        finally:
            r.close()


class SpatialStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.store = Store(30, 100_000, clock=lambda: self.now[0])

    def tearDown(self):
        self.store.close()

    def test_negative_cells_floor(self):
        self.assertEqual(self.store.cell_of(-0.01, -0.01, -0.01), (-1, -1, -1))
        self.assertEqual(self.store.cell_of(-32, 0, 31.99), (-1, 0, 0))
        self.assertEqual(self.store.cell_of(-32.01, 16, 32), (-2, 1, 1))

    def test_query_plan_uses_spatial_key(self):
        for i in range(500):
            _, s = self.store.create_session(presence=True)
            self.store.update_presence(s, REALM, PROFILE, "inst-%012d" % i, i * 3.0, 64, -i * 3.0)
        self.store.db.execute("ANALYZE")
        plan = " | ".join(self.store.explain_near())
        self.assertIn("USING PRIMARY KEY (realm=? AND profile=? AND cx=? AND cz=? AND cy>? AND cy<?)", plan)
        self.assertNotIn("SCAN", plan)

    def test_matches_brute_force_across_cell_borders(self):
        rng = random.Random(7)
        points = []
        for i in range(800):
            _, s = self.store.create_session(presence=True)
            x, y, z = rng.uniform(-200, 200), rng.uniform(-64, 200), rng.uniform(-200, 200)
            profile = PROFILE if i % 4 else "gen1-other0000"
            self.store.update_presence(s, REALM, profile, "inst-%012d" % i, x, y, z)
            points.append((s, profile, x, y, z))
        store = self.store
        store.near_limit = 10_000  # compare full result sets
        store._near_sql = store._build_near_sql()
        for s, profile, x, y, z in points[:200]:
            _, got = store.update_presence(s, REALM, profile, "inst-%012d" % (s.sid - 1), x, y, z)
            expected = sorted(
                ((ox - x) ** 2 + (oz - z) ** 2, round(ox, 6))
                for o, op, ox, oy, oz in points
                if o.sid != s.sid and op == profile
                and (ox - x) ** 2 + (oz - z) ** 2 <= 48 ** 2 and abs(oy - y) <= 12
            )
            self.assertEqual([round(n.x, 6) for n in got], [e[1] for e in expected])


if __name__ == "__main__":
    unittest.main()
