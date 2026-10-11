import json
import unittest

from noctis_relay.store import Store

from .support import RunningRelay, make_config

REALM = "astralyth:noctis"
PROFILE = "terrain-0123456789abcdef"
ACCOUNT = "0f1e2d3c-4b5a-4968-8776-655443322110"


def place(x=0.5, y=64.0, z=0.5, instance="inst-aaaaaaaaaaaa"):
    return {"realm": REALM, "profile": PROFILE, "instance": instance, "x": x, "y": y, "z": z}


def walk(n=6, step=8, flags=1):
    """A short straight walk: ``step`` 1/32 block per 2 ticks."""
    return [[2 * i, step * i, 0, 0, 64, 0, 64, flags] for i in range(n)]


def clip(instance="inst-aaaaaaaaaaaa", frames=None, **extra):
    body = {**place(instance=instance), "account": ACCOUNT, "frames": frames or walk()}
    body.update(extra)
    return body


class EchoApiTest(unittest.TestCase):
    def setUp(self):
        self.r = RunningRelay(make_config(
            echoes_enabled=True, echo_chat_enabled=True,
            echo_publish_interval=1.0, echo_fetch_interval=1.0,
            global_echo_rate=0.0,
        ))

    def tearDown(self):
        self.r.close()

    def session(self, chat=False):
        return self.r.create(capabilities=["echoes", "chat"] if chat else ["echoes"])

    def publish(self, token, body):
        return self.r.request("PUT", "/v1/echoes/clip", body, token=token)

    def fetch(self, token, instance="inst-bbbbbbbbbbbb", **at):
        return self.r.request("POST", "/v1/echoes/next", place(instance=instance, **at), token=token)

    def test_info_and_capability_negotiation(self):
        _, info, _ = self.r.request("GET", "/v1/info")
        self.assertEqual(info["privacyRevision"], 3)
        self.assertEqual(info["capabilities"], ["sessions", "echoes", "chat"])
        self.assertTrue(info["echoes"]["chat"])
        status, body, _ = self.r.request("POST", "/v1/sessions", {"protocol": 1, "capabilities": ["chat"]})
        self.assertEqual((status, body["error"]), (400, "invalid_capabilities"))
        status, body, _ = self.r.request(
            "POST", "/v1/sessions", {"protocol": 1, "capabilities": ["chat", "echoes"]})
        self.assertEqual(body["capabilities"], ["echoes", "chat"])
        plain = self.r.create()
        self.assertEqual(self.publish(plain, clip())[1]["error"], "capability_not_granted")
        self.assertEqual(self.fetch(plain)[0], 403)

    def test_disabled_relay_refuses_echo_capability(self):
        r = RunningRelay(make_config())
        try:
            status, body, _ = r.request("POST", "/v1/sessions", {"protocol": 1, "capabilities": ["echoes"]})
            self.assertEqual((status, body["error"]), (400, "capability_unavailable"))
            self.assertEqual(r.request("PUT", "/v1/echoes/clip", clip(), token=r.create())[0], 404)
        finally:
            r.close()

    def test_chat_needs_its_own_capability_on_relay(self):
        r = RunningRelay(make_config(echoes_enabled=True))
        try:
            status, body, _ = r.request(
                "POST", "/v1/sessions", {"protocol": 1, "capabilities": ["echoes", "chat"]})
            self.assertEqual((status, body["error"]), (400, "capability_unavailable"))
        finally:
            r.close()

    def test_round_trip_once_and_other_instance_only(self):
        a, b, same = self.session(), self.session(), self.session()
        status, body, _ = self.publish(a, clip())
        self.assertEqual((status, body["chat"]), (200, 0))
        # Same game instance (shared server): never offered.
        self.assertIsNone(self.fetch(same, instance="inst-aaaaaaaaaaaa")[1]["echo"])
        # Own clip: never offered.
        self.assertIsNone(self.fetch(a, instance="inst-cccccccccccc")[1]["echo"])
        status, body, _ = self.fetch(b)
        echo = body["echo"]
        self.assertEqual(status, 200)
        self.assertEqual(echo["account"], ACCOUNT)
        self.assertEqual(echo["frames"], walk())
        self.assertEqual(echo["actions"], [])
        self.assertNotIn("chat", echo)
        self.assertRegex(echo["ref"], r"^[0-9a-f]{16}$")
        self.assertEqual(set(echo), {"ref", "ageSeconds", "x", "y", "z", "account", "frames", "actions"})
        # Delivered at most once per observer.
        self.r.clock.advance(2)
        self.assertIsNone(self.fetch(b)[1]["echo"])

    def test_distance_profile_and_realm_filters(self):
        a, b = self.session(), self.session()
        self.publish(a, clip())
        self.assertIsNone(self.fetch(b, x=200.0)[1]["echo"])
        self.r.clock.advance(2)
        self.assertIsNone(self.fetch(b, y=120.0)[1]["echo"])
        self.r.clock.advance(2)
        other = place(instance="inst-bbbbbbbbbbbb")
        other["profile"] = "terrain-ffffffffffffffff"
        self.assertIsNone(self.r.request("POST", "/v1/echoes/next", other, token=b)[1]["echo"])

    def test_origin_preserves_block_boundaries(self):
        # Block action offsets use floor(origin), so even millimetre rounding
        # changes their world block near a positive or negative boundary.
        a, b = self.session(), self.session()
        for x, y, z in ((0.9998, 64.9998, -0.0002),
                        (-1.0002, -0.0002, 1.9998),
                        (29_999_999.9998, 4095.9998, -29_999_999.9998)):
            self.r.clock.advance(2)
            body = clip(x=x, y=y, z=z, actions=[[2, 2, 1, 0, 0, "minecraft:stone"]])
            self.assertEqual(self.publish(a, body)[0], 200)
            status, payload, _ = self.fetch(b, x=x, y=y, z=z)
            self.assertEqual(status, 200)
            self.assertEqual([payload["echo"][k] for k in ("x", "y", "z")], [x, y, z])

    def test_withdraw_and_logout_remove_clip_immediately(self):
        a, b, c = self.session(), self.session(), self.session()
        self.publish(a, clip())
        self.assertEqual(self.r.request("DELETE", "/v1/echoes", token=a)[0], 204)
        self.assertIsNone(self.fetch(b)[1]["echo"])
        self.r.clock.advance(2)
        self.publish(a, clip())
        self.assertEqual(self.r.request("DELETE", "/v1/session", token=a)[0], 204)
        self.assertIsNone(self.fetch(c)[1]["echo"])
        self.assertEqual(self.r.call(self.r.relay.store.clip_count), 0)

    def test_expired_source_session_excluded_before_purge(self):
        # Session retention (30 s) is shorter than the clip TTL (60 s).
        a, b = self.session(), self.session()
        self.publish(a, clip())
        store = self.r.relay.store
        # Freeze purge so only the query decides.
        self.r.call(lambda: setattr(store, "purge", lambda *a, **k: 0))
        self.r.clock.advance(31)
        b2 = self.session()
        self.assertIsNone(self.fetch(b2)[1]["echo"])
        self.assertEqual(self.r.call(store.clip_count), 1)

    def test_pacing(self):
        a, b = self.session(), self.session()
        self.assertEqual(self.publish(a, clip())[0], 200)
        status, body, headers = self.publish(a, clip())
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        self.assertIn("retry-after", headers)
        self.fetch(b)
        self.assertEqual(self.fetch(b)[0], 429)

    def test_chat_replayed_only_to_chat_sessions(self):
        talker, quiet_observer, chat_observer, no_chat = (
            self.session(chat=True), self.session(), self.session(chat=True), self.session())
        self.assertEqual(self.publish(no_chat, clip(chat=[[2, "salut"]]))[1]["error"], "capability_not_granted")
        status, body, _ = self.publish(talker, clip(chat=[[2, "salut à tous"]]))
        self.assertEqual((status, body["chat"]), (200, 1))
        self.assertNotIn("chat", self.fetch(quiet_observer)[1]["echo"])
        self.assertEqual(self.fetch(chat_observer, instance="inst-dddddddddddd")[1]["echo"]["chat"],
                         [[2, "salut à tous"]])

    def test_chat_filters(self):
        t = self.session(chat=True)
        bad = ["a‮b", "a\nb", "§cred", "x" * 65, "", " lead", "a​b", "a\x7fb"]
        for text in bad:
            status, body, _ = self.publish(t, clip(chat=[[2, text]]))
            self.assertEqual((status, body["error"]), (400, "invalid_chat"), repr(text))
        # Past the end of the clip: could never be shown.
        self.assertEqual(self.publish(t, clip(chat=[[11, "trop tard"]]))[1]["error"], "invalid_chat")
        self.assertEqual(self.publish(t, clip(chat=[[1, "a"], [2, "b"], [3, "c"]]))[0], 400)
        self.assertEqual(self.publish(t, clip(chat=[[1, True]]))[0], 400)
        # Silently dropped: links, repeats, duplicates.
        dropped = ["voir https://evil.example", "www.example.org", "rejoins discord.gg/abc",
                   "join example.com now", "aaaaaaaaaaaa", "@someone123"]
        for text in dropped:
            self.r.clock.advance(2)
            status, body, _ = self.publish(t, clip(chat=[[2, text]]))
            self.assertEqual((status, body["chat"]), (200, 0), text)
        self.r.clock.advance(2)
        self.assertEqual(self.publish(t, clip(chat=[[2, "bonjour"], [4, "BONJOUR"]]))[1]["chat"], 1)
        self.r.clock.advance(2)
        self.assertEqual(self.publish(t, clip(chat=[[2, "bonjour"]]))[1]["chat"], 0)

    def test_frame_validation(self):
        a = self.session()
        cases = {
            "one frame": [[0, 0, 0, 0, 0, 0, 0, 0]],
            "not starting at origin": [[0, 1, 0, 0, 0, 0, 0, 0], [2, 2, 0, 0, 0, 0, 0, 0]],
            "time not increasing": [[0, 0, 0, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0, 0, 0]],
            "too long": [[0, 0, 0, 0, 0, 0, 0, 0], [121, 0, 0, 0, 0, 0, 0, 0]],
            "float": [[0, 0, 0, 0, 0, 0, 0, 0], [2, 1.5, 0, 0, 0, 0, 0, 0]],
            "bool": [[0, 0, 0, 0, 0, 0, 0, 0], [2, True, 0, 0, 0, 0, 0, 0]],
            "yaw": [[0, 0, 0, 0, 256, 0, 0, 0], [2, 0, 0, 0, 0, 0, 0, 0]],
            "pitch": [[0, 0, 0, 0, 0, 65, 0, 0], [2, 0, 0, 0, 0, 0, 0, 0]],
            "flags": [[0, 0, 0, 0, 0, 0, 0, 32], [2, 0, 0, 0, 0, 0, 0, 0]],
            "short": [[0, 0, 0, 0, 0, 0, 0], [2, 0, 0, 0, 0, 0, 0]],
            "offset": [[0, 0, 0, 0, 0, 0, 0, 0], [120, 1537, 0, 0, 0, 0, 0, 0]],
            "too many": [[i, 0, 0, 0, 0, 0, 0, 0] for i in range(61)],
            "string": [[0, 0, 0, 0, 0, 0, 0, 0], [2, "1", 0, 0, 0, 0, 0, 0]],
        }
        for name, frames in cases.items():
            status, body, _ = self.publish(a, clip(frames=frames))
            self.assertEqual((status, body["error"]), (400, "invalid_frames"), name)
        # Teleport and fly-hacks.
        for frames in (
            [[0, 0, 0, 0, 0, 0, 0, 0], [1, 64, 0, 0, 0, 0, 0, 0]],       # 2 blocks in one tick
            [[0, 0, 0, 0, 0, 0, 0, 0], [1, 0, 160, 0, 0, 0, 0, 0]],      # 5 blocks up in one tick
        ):
            self.assertEqual(self.publish(a, clip(frames=frames))[1]["error"], "implausible_movement")

    def test_action_validation(self):
        a = self.session()
        ok = clip(actions=[[2, 1, 1, 0, 0, "minecraft:stone"], [4, 2, 0, -1, 2, "astralyth:noctis_moss"]])
        self.assertEqual(self.publish(a, ok)[0], 200)
        for actions, code in (
            ([[2, 1, 30, 0, 0, "minecraft:stone"]], "implausible_action"),
            ([[2, 3, 0, 0, 0, "minecraft:stone"]], "invalid_actions"),
            ([[2, 1, 0, 0, 0, "Minecraft:Stone"]], "invalid_block"),
            ([[2, 1, 0, 0, 0, "stone"]], "invalid_block"),
            ([[2, 1, 0, 0, 0, "minecraft:stone[lit=true]"]], "invalid_block"),
            ([[2, 1, 0, 0, 0, "minecraft:stone{a:1}"]], "invalid_block"),
            ([[99, 1, 0, 0, 0, "minecraft:stone"]], "invalid_actions"),
            ([[2, 1, 0, 0, 0, "minecraft:stone"]] * 9, "invalid_actions"),
        ):
            self.r.clock.advance(2)
            status, body, _ = self.publish(a, clip(actions=actions))
            self.assertEqual((status, body["error"]), (400, code), actions)

    def test_identity_and_unknown_fields(self):
        a = self.session()
        for account in ("not-a-uuid", ACCOUNT.upper(), ACCOUNT + "0", "§c" + ACCOUNT[2:]):
            self.assertEqual(self.publish(a, clip(account=account))[1]["error"], "invalid_account")
        self.assertEqual(self.publish(a, clip(name="Notch"))[1]["error"], "invalid_fields")
        self.assertEqual(self.publish(a, clip(skin="http://evil/skin.png"))[1]["error"], "invalid_fields")

    def test_action_reach_uses_exact_origin(self):
        a = self.session()
        stationary = [[0, 0, 0, 0, 64, 0, 64, 1], [2, 0, 0, 0, 64, 0, 64, 1]]
        # A block 6.49 blocks from the eyes is reachable; ignoring the origin's
        # fractional part would incorrectly put its centre beyond 6.5.
        body = clip(x=0.01, y=64.99, z=0.5, frames=stationary,
                    actions=[[2, 1, 6, 2, 0, "minecraft:stone"]])
        self.assertEqual(self.publish(a, body)[0], 200)
        self.r.clock.advance(2)
        # Conversely, a block on the other side exceeds 6.5 in real space.
        body = clip(x=0.99, y=64.99, z=0.99, frames=stationary,
                    actions=[[2, 1, -6, 1, 0, "minecraft:stone"]])
        self.assertEqual(self.publish(a, body)[1]["error"], "implausible_action")

    def test_route_body_limit(self):
        a = self.session()
        big = clip()
        big["actions"] = []
        raw = json.dumps(big).encode() + b" " * 7000
        status, body, _ = self.r.request("PUT", "/v1/echoes/clip", token=a, raw_body=raw,
                                         headers={"Content-Type": "application/json"})
        self.assertEqual(status, 413)
        # The larger limit applies to clip uploads only.
        raw = json.dumps(place()).encode() + b" " * 2000
        status, _, _ = self.r.request("POST", "/v1/echoes/next", token=a, raw_body=raw,
                                      headers={"Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_maximal_clip_fits_body_limit(self):
        a = self.session(chat=True)
        frames = [[2 * i, -1536 if i else 0, 0, 0, 255, -64, 255, 31] for i in range(60)]
        frames[1:] = [[2 * i, -24 * i, -2 * i, -24 * i, 255, -64, 255, 31] for i in range(1, 60)]
        actions = [[i * 4, 1, -1 - i // 4, -1, -2, "abcdefghijklmnopqrstuvwxyz012345:" + "p" * 64]
                   for i in range(8)]
        actions = [[t, k, round(frames[t // 2][1] / 32), 0, round(frames[t // 2][3] / 32), b]
                   for t, k, _, _, _, b in actions]
        body = clip(frames=frames, actions=actions, chat=[[2, "\U0001F600" * 64], [4, "ü" * 64]])
        body.update(realm="abcdefghijklmnopqrstuvwxyz012345:" + "p" * 64, profile="p" * 64, instance="i" * 64,
                    x=-29999999.123456789, y=-2047.123456789, z=-29999999.123456789)
        # Worst case even with every non-ASCII character escaped by the client.
        size = len(json.dumps(body, separators=(",", ":")).encode())
        self.assertLess(size, 6144)
        status, resp, _ = self.publish(a, body)
        self.assertEqual(status, 200, resp)


class EchoStoreTest(unittest.TestCase):
    def test_per_cell_cap_and_bounded_lookup(self):
        now = [0.0]
        s = Store(30, 10_000, clock=lambda: now[0], echo_max_per_cell=4)
        sessions = [s.create_session(echoes=True)[1] for _ in range(6)]
        for i, sess in enumerate(sessions[:4]):
            s.publish_clip(sess, "r", "p", f"i{i}", 1, 64, 1, "{}", None, None)
        with self.assertRaises(Exception):
            s.publish_clip(sessions[4], "r", "p", "i4", 2, 64, 2, "{}", None, None)
        # Replacing one's own clip in a full cell is allowed.
        s.publish_clip(sessions[0], "r", "p", "i0", 3, 64, 3, "{}", None, None)
        self.assertEqual(s.clip_count(), 4)
        plan = " ".join(s.explain_echo())
        self.assertIn("USING PRIMARY KEY", plan)
        self.assertNotIn("SCAN clip", plan)
        self.assertNotIn("SCAN session", plan)

    def test_expired_source_session_is_never_served(self):
        now = [0.0]
        s = Store(30, 100, clock=lambda: now[0], echo_ttl=60)
        _, src = s.create_session(echoes=True)
        s.publish_clip(src, "r", "p", "i1", 0, 64, 0, "{}", None, None)
        now[0] = 31.0
        _, obs = s.create_session(echoes=True)
        self.assertIsNone(s.next_echo(obs, "r", "p", "i2", 1, 64, 1))
        self.assertEqual(s.clip_count(), 1)  # not yet purged
        s.purge()
        self.assertEqual(s.clip_count(), 0)

    def test_observer_delivery_rows_bounded_and_dropped_with_session(self):
        now = [0.0]
        s = Store(30, 100, clock=lambda: now[0])
        tok, obs = s.create_session(echoes=True)
        for i in range(5):
            _, src = s.create_session(echoes=True)
            s.publish_clip(src, "r", "p", f"i{i}", i, 64, 0, "{}", None, None)
            self.assertIsNotNone(s.next_echo(obs, "r", "p", "obs", 0, 64, 0))
        self.assertEqual(s.db.execute("SELECT count(*) FROM delivered").fetchone()[0], 5)
        s.delete_session(tok)
        self.assertEqual(s.db.execute("SELECT count(*) FROM delivered").fetchone()[0], 0)
