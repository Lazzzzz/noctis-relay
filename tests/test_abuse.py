"""Hostile scenarios against a real relay over localhost TCP.

Different client networks are emulated through X-Forwarded-For from the
trusted local proxy (127.0.0.1), exactly as Caddy would forward them.
"""

import ipaddress
import logging
import os
import socket
import tempfile
import time
import unittest

from noctis_relay.config import ConfigError, load_config
from noctis_relay.ratelimit import NetworkSet, parse_networks

from .support import RunningRelay, make_config

LOCAL_PROXY = (ipaddress.ip_network("127.0.0.1/32"),)
A, B, C = "198.51.100.10", "203.0.113.20", "192.0.2.30"
REALM = "astralyth:noctis"
PROFILE = "gen1-a1b2c3d4"


def proxied(**overrides):
    return make_config(trust_proxy=True, trusted_proxies=LOCAL_PROXY, **overrides)


def pos(x, y, z, instance):
    return {"realm": REALM, "profile": PROFILE, "instance": instance, "x": x, "y": y, "z": z}


def status_of(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1]) if raw else 0


def recv_all(sock) -> bytes:
    data = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return data
            data += chunk
    except (ConnectionResetError, socket.timeout):
        return data


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.02)


def ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


class RelayCase(unittest.TestCase):
    config = None

    def setUp(self):
        self.r = RunningRelay(self.config)

    def tearDown(self):
        self.assertEqual(self.r.server.errors, 0)  # never a 500 or crash
        self.r.close()

    def session_row(self, token):
        from noctis_relay.store import hash_token

        return self.r.call(lambda: self.r.relay.store.db.execute(
            "SELECT expires, net, last_presence FROM session WHERE token_hash = ?", (hash_token(token),)
        ).fetchone())


class ActiveSessionCapTest(RelayCase):
    config = proxied(max_sessions_per_network=3)

    def try_create(self, via):
        return self.r.request("POST", "/v1/sessions", {"protocol": 1}, via=via)

    def test_cap_per_network_and_release(self):
        tokens = [self.r.create(via=A) for _ in range(3)]
        status, body, headers = self.try_create(A)
        self.assertEqual((status, body["error"], headers["retry-after"]), (429, "too_many_sessions", "30"))
        # Other networks are unaffected.
        self.r.create(via=B)
        # DELETE frees a slot at once (client disconnect).
        self.assertEqual(self.r.request("DELETE", "/v1/session", token=tokens[0], via=A)[0], 204)
        tokens[0] = self.r.create(via=A)
        self.assertEqual(self.try_create(A)[0], 429)
        # Expired sessions stop counting immediately, before any purge pass.
        self.r.server._purger.cancel()
        self.r.clock.advance(31)
        for _ in range(3):
            self.r.create(via=A)

    def test_heartbeats_keep_the_slot(self):
        token = self.r.create(via=A)
        self.r.create(via=A)
        self.r.create(via=A)
        for _ in range(4):
            self.r.clock.advance(20)
            self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=token, via=A)[0], 200)
        # Only the heartbeated session is still alive: two slots are free.
        self.r.create(via=A)
        self.r.create(via=A)
        self.assertEqual(self.try_create(A)[0], 429)

    def test_ipv6_grouped_by_64(self):
        for i in range(3):
            self.r.create(via=f"2001:db8:1:2::{i + 1}")
        self.assertEqual(self.try_create("2001:db8:1:2:ffff::9")[0], 429)  # same /64
        self.r.create(via="2001:db8:1:3::1")                                 # next /64
        self.r.create(via="::ffff:" + B)                                     # mapped IPv4 = B

    def test_forwarded_header_ignored_from_untrusted_peer(self):
        r = RunningRelay(make_config(max_sessions_per_network=2))  # trust_proxy off
        try:
            r.create(via=A)
            r.create(via=B)
            status, body, _ = r.request("POST", "/v1/sessions", {"protocol": 1}, via=C)
            self.assertEqual((status, body["error"]), (429, "too_many_sessions"))
        finally:
            r.close()


class SessionNetworkTest(RelayCase):
    config = proxied(max_sessions_per_network=2)

    def test_session_follows_legitimate_network_change(self):
        token = self.r.create(via=A)
        before = self.session_row(token)[1]
        self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=token, via=B)[0], 200)
        after = self.session_row(token)[1]
        self.assertNotEqual(before, after)
        # The slot moved: A has room for two, B for one more.
        self.r.create(via=A)
        self.r.create(via=A)
        self.r.create(via=B)
        self.assertEqual(self.r.request("POST", "/v1/sessions", {"protocol": 1}, via=B)[0], 429)

    def test_transferred_tokens_cannot_bypass_the_cap(self):
        # Sessions opened through many rotating addresses, then all used
        # from one host: only the cap of that host's network is honoured.
        tokens = [self.r.create(via=f"198.18.{i}.1") for i in range(6)]
        statuses = [self.r.request("PUT", "/v1/session", {}, token=t, via=C)[0] for t in tokens]
        self.assertEqual(statuses, [200, 200, 429, 429, 429, 429])

    def test_replay_from_full_network_changes_nothing(self):
        victim = self.r.create(via=A)
        self.r.create(via=C)
        self.r.create(via=C)
        row = self.session_row(victim)
        self.r.clock.advance(5)
        status, body, headers = self.r.request("PUT", "/v1/session", {}, token=victim, via=C)
        self.assertEqual((status, body["error"], headers["retry-after"]), (429, "too_many_sessions", "30"))
        self.assertEqual(self.session_row(victim), row)  # expiry and network untouched
        self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=victim, via=A)[0], 200)

    def test_bad_auth_mutates_nothing(self):
        token = self.r.create(via=A)
        row = self.session_row(token)
        for header in ("Bearer " + "0" * 64, "Bearer " + token.upper(), "Bearer " + token[:-1], "Basic " + token):
            status, _, _ = self.r.request("PUT", "/v1/session", {}, headers={"Authorization": header}, via=B)
            self.assertEqual(status, 401, header)
            self.assertEqual(self.r.request("DELETE", "/v1/session", headers={"Authorization": header}, via=B)[0] in (204, 401), True)
        self.assertEqual(self.session_row(token), row)


class StrictBindingTest(RelayCase):
    config = proxied(max_sessions_per_network=2, session_binding="strict")

    def test_foreign_replay_cannot_spend_holders_request_budget(self):
        r = RunningRelay(proxied(session_binding="strict", session_request_rate=0.001,
                                 session_request_burst=2))
        try:
            token = r.create(via=A)
            for _ in range(5):
                self.assertEqual(r.request("PUT", "/v1/session", {}, token=token, via=B)[0], 401)
            # Both units remain available to the holder after all replays.
            for _ in range(2):
                self.assertEqual(r.request("PUT", "/v1/session", {}, token=token, via=A)[0], 200)
            self.assertEqual(r.request("PUT", "/v1/session", {}, token=token, via=A)[0], 429)
            self.assertEqual(r.server.errors, 0)
        finally:
            r.close()

    def test_other_network_cannot_use_the_token(self):
        token = self.r.create(via=A)
        row = self.session_row(token)
        status, body, _ = self.r.request("PUT", "/v1/session", {}, token=token, via=B)
        self.assertEqual((status, body["error"]), (401, "session_expired"))
        self.assertEqual(self.session_row(token), row)
        self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=token, via=A)[0], 200)
        # Revocation still works from anywhere and frees A's slot.
        self.assertEqual(self.r.request("DELETE", "/v1/session", token=token, via=B)[0], 204)
        self.assertIsNone(self.session_row(token))
        self.r.create(via=A)
        self.r.create(via=A)


class GlobalBudgetTest(unittest.TestCase):
    def test_rotating_addresses_hit_the_global_request_budget(self):
        r = RunningRelay(proxied(request_rate=1.0, request_burst=2, global_request_rate=0.5, global_request_burst=5))
        try:
            # A throttled client does not drain the shared budget.
            self.assertEqual([r.request("GET", "/healthz", via=A)[0] for _ in range(4)], [200, 200, 429, 429])
            statuses = [r.request("GET", "/healthz", via=f"198.18.0.{i}")[0] for i in range(1, 6)]
            self.assertEqual(statuses, [200, 200, 200, 503, 503])
            status, body, headers = r.request("GET", "/healthz", via=B)
            self.assertEqual((status, body["error"]), (503, "overloaded"))
            self.assertGreaterEqual(int(headers["retry-after"]), 1)
            r.clock.advance(2)
            self.assertEqual(r.request("GET", "/healthz", via=B)[0], 200)
            self.assertLessEqual(len(r.relay.requests), r.relay.config.rate_table_size)
        finally:
            r.close()

    def test_global_and_prefix_creation_budgets(self):
        r = RunningRelay(proxied(global_create_rate=0.01, global_create_burst=5,
                                 create_prefix_rate=0.01, create_prefix_burst=2))
        try:
            r.create(via="198.51.100.1")
            r.create(via="198.51.100.2")
            status, body, _ = r.request("POST", "/v1/sessions", {"protocol": 1}, via="198.51.100.3")  # same /24
            self.assertEqual((status, body["error"]), (429, "rate_limited"))
            r.create(via="2001:db8:aa:1::1")
            status, _, _ = r.request("POST", "/v1/sessions", {"protocol": 1}, via="2001:db8:aa:2::1")  # same /48
            r.create(via="203.0.113.1")
            self.assertEqual(status, 201)
            status, body, headers = r.request("POST", "/v1/sessions", {"protocol": 1}, via="192.0.2.1")
            self.assertEqual((status, body["error"]), (503, "overloaded"))
            self.assertIn("retry-after", headers)
            # Heartbeats of existing sessions are not creation-limited.
            self.assertEqual(r.call(r.relay.store.session_count), 5)
        finally:
            r.close()

    def test_anonymous_flood_cannot_starve_live_sessions(self):
        r = RunningRelay(proxied(global_request_rate=0.001, global_request_burst=4))
        try:
            players = [r.create(via=A), r.create(via=B)]  # two anonymous requests
            flood = [r.request("GET", "/v1/info", via=f"198.18.1.{i}")[0] for i in range(1, 5)]
            self.assertEqual(flood, [200, 200, 503, 503])
            for _ in range(5):
                self.assertEqual(r.request("PUT", "/v1/session", {}, token=players[0], via=A)[0], 200)
                self.assertEqual(r.request("PUT", "/v1/session", {}, token=players[1], via=B)[0], 200)
            # Unknown or expired tokens are anonymous traffic.
            self.assertEqual(r.request("PUT", "/v1/session", {}, token="0" * 64, via=C)[0], 503)
            self.assertEqual(r.request("DELETE", "/v1/session", token=players[1], via=B)[0], 204)
            self.assertEqual(r.request("PUT", "/v1/session", {}, token=players[1], via=B)[0], 503)
        finally:
            r.close()

    def test_per_session_budget(self):
        r = RunningRelay(proxied(session_request_rate=0.01, session_request_burst=3))
        try:
            noisy, quiet = r.create(via=A), r.create(via=A)
            statuses = [r.request("PUT", "/v1/session", {}, token=noisy, via=A)[0] for _ in range(4)]
            self.assertEqual(statuses, [200, 200, 200, 429])
            self.assertEqual(r.request("PUT", "/v1/session", {}, token=quiet, via=A)[0], 200)
            # The holder of a throttled token may still revoke it later.
            r.clock.advance(100)
            self.assertEqual(r.request("DELETE", "/v1/session", token=noisy, via=A)[0], 204)
        finally:
            r.close()

    def test_refused_creations_are_refunded(self):
        r = RunningRelay(proxied(create_rate=0.001, create_burst=2, global_create_rate=0.01, global_create_burst=1,
                                 max_sessions_per_network=1))
        try:
            r.create(via=A)
            # Network cap: 429 too_many_sessions, without burning tokens.
            for _ in range(3):
                status, body, _ = r.request("POST", "/v1/sessions", {"protocol": 1}, via=A)
                self.assertEqual((status, body["error"]), (429, "too_many_sessions"))
            # Global shortage: 503 every time, the client's own budget is kept.
            for _ in range(3):
                self.assertEqual(r.request("POST", "/v1/sessions", {"protocol": 1}, via=B)[0], 503)
            r.clock.advance(100)  # global budget back to 1; A's session expired
            r.create(via=B)
            r.clock.advance(100)
            r.create(via=A)       # A still had its second creation token
        finally:
            r.close()

    def test_throttled_client_is_cut_before_its_body(self):
        r = RunningRelay(make_config(request_rate=0.01, request_burst=1, body_timeout=5))
        try:
            self.assertEqual(r.request("GET", "/healthz")[0], 200)
            start = time.monotonic()
            with socket.create_connection(("127.0.0.1", r.port), timeout=3) as sock:
                sock.sendall(b"POST /v1/sessions HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n")
                data = recv_all(sock)
            self.assertEqual(status_of(data), 429)
            self.assertIn(b"Connection: close", data)
            self.assertLess(time.monotonic() - start, 1.5)  # no wait for the body
        finally:
            r.close()


class SlowClientTest(unittest.TestCase):
    def test_one_peer_cannot_hold_every_socket(self):
        if not ipv6_loopback():
            self.skipTest("no IPv6 loopback")
        r = RunningRelay(make_config(max_connections=50, max_connections_per_peer=3, header_timeout=3), dual_stack=True)
        try:
            idle = [socket.create_connection(("127.0.0.1", r.port)) for _ in range(3)]
            time.sleep(0.2)
            with socket.create_connection(("127.0.0.1", r.port), timeout=2) as extra:
                try:
                    extra.sendall(b"GET /healthz HTTP/1.1\r\n\r\n")
                except OSError:
                    pass
                self.assertEqual(recv_all(extra), b"")
            # A different peer is still served while the first one stalls.
            self.assertEqual(r.request("GET", "/healthz", host="::1")[0], 200)
            for s in idle:
                s.close()
            time.sleep(0.2)
            self.assertEqual(r.request("GET", "/healthz")[0], 200)
            wait_for(lambda: len(r.server.peer_sockets) == 0)  # no leaked entries
        finally:
            r.close()

    def test_slow_bodies_behind_proxy_limited_per_real_client(self):
        cfg = proxied(max_connections_per_peer=1, max_inflight_per_client=2, body_timeout=1.5)
        r = RunningRelay(cfg)
        try:
            slow = []
            for _ in range(2):
                s = socket.create_connection(("127.0.0.1", r.port), timeout=3)
                s.sendall(b"POST /v1/sessions HTTP/1.1\r\nX-Forwarded-For: " + A.encode() +
                          b"\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{")
                slow.append(s)
            time.sleep(0.2)
            # The proxy itself is exempt from the per-peer socket cap.
            status, body, headers = r.request("GET", "/healthz", via=A)
            self.assertEqual((status, body["error"], headers["retry-after"]), (429, "concurrency_limited", "1"))
            self.assertEqual(r.request("GET", "/healthz", via=B)[0], 200)
            for s in slow:  # cut at body_timeout, slots released
                self.assertEqual(recv_all(s), b"")
                s.close()
            self.assertEqual(r.request("GET", "/healthz", via=A)[0], 200)
            wait_for(lambda: len(r.server.inflight) == 0)
        finally:
            r.close()


class DenyListTest(unittest.TestCase):
    def test_direct_peer_denied_without_answer(self):
        r = RunningRelay(make_config(deny_networks=(ipaddress.ip_network("127.0.0.0/8"),)))
        try:
            with socket.create_connection(("127.0.0.1", r.port), timeout=2) as sock:
                try:
                    sock.sendall(b"GET /healthz HTTP/1.1\r\n\r\n")
                except OSError:
                    pass
                self.assertEqual(recv_all(sock), b"")
        finally:
            r.close()

    def test_proxied_client_denied_and_file_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deny.txt")
            with open(path, "w") as fh:
                fh.write("# abusive hosting range\n203.0.113.0/24\n")
            r = RunningRelay(proxied(deny_file=path, deny_networks=(ipaddress.ip_network("2001:db8:bad::/48"),)))
            try:
                token = r.create(via=A)
                for via in (B, "2001:db8:bad:1::5", "::ffff:" + B):
                    status, body, headers = r.request("GET", "/v1/info", via=via)
                    self.assertEqual((status, body["error"], headers["connection"]), (403, "forbidden", "close"), via)
                with open(path, "w") as fh:
                    fh.write(A + "\n")
                with self.assertLogs("noctis_relay", level=logging.INFO) as logs:
                    r.call(r.server.reload_deny)
                self.assertIn("deny list reloaded: 2 networks", logs.output[0])
                self.assertEqual(r.request("PUT", "/v1/session", {}, token=token, via=A)[0], 403)
                self.assertEqual(r.request("GET", "/healthz", via=B)[0], 200)
                with open(path, "w") as fh:
                    fh.write("198.51.100.77/99\n")
                with self.assertLogs("noctis_relay", level=logging.ERROR) as logs:
                    r.call(r.server.reload_deny)
                self.assertNotIn("198.51", "".join(logs.output))
                self.assertEqual(r.request("GET", "/healthz", via=A)[0], 403)  # old list kept
            finally:
                r.close()

    def test_network_set(self):
        nets = parse_networks("10.0.0.0/8, 192.0.2.7\n2001:db8::/32 # comment\n::ffff:198.51.100.0/120", "test")
        deny = NetworkSet(nets)
        for ip, hit in [("10.200.3.4", True), ("11.0.0.1", False), ("192.0.2.7", True), ("192.0.2.8", False),
                        ("2001:db8:ffff::1", True), ("2001:db9::1", False), ("198.51.100.200", True),
                        ("::ffff:10.1.1.1", True), (None, False)]:
            self.assertEqual(ip in deny if ip is None else ipaddress.ip_address(ip) in deny, hit, ip)
        big = NetworkSet(ipaddress.ip_network(f"100.{i // 256}.{i % 256}.0/24") for i in range(20_000))
        start = time.perf_counter()
        for _ in range(10_000):
            ipaddress.ip_address("100.64.1.1") in big
        self.assertLess(time.perf_counter() - start, 1.0)  # one lookup per prefix length
        with self.assertRaises(ConfigError) as ctx:
            parse_networks("10.0.0.0/8\nnot-an-ip", "NOCTIS_DENY_NETWORKS")
        self.assertNotIn("not-an-ip", str(ctx.exception))


class HostilePresenceTest(RelayCase):
    config = proxied(presence_enabled=True, presence_min_interval=1.0, presence_max_per_cell=3,
                     max_sessions_per_network=50)

    def put(self, token, body, via=A):
        return self.r.request("PUT", "/v1/presence", body, token=token, via=via)

    def test_dense_cell_is_capped_without_mutation(self):
        players = [self.r.create(via=A, capabilities=["presence"]) for _ in range(5)]
        for i, t in enumerate(players[:3]):
            self.assertEqual(self.put(t, pos(i + 1, 64, i + 1, "inst-%012d" % i))[0], 200)
        late = players[3]
        before = self.session_row(late)
        status, body, headers = self.put(late, pos(4, 64, 4, "inst-late00000000"))
        self.assertEqual((status, body["error"], headers["retry-after"]), (503, "area_full", "30"))
        self.assertEqual(self.session_row(late), before)  # no expiry, interval or network change
        self.assertEqual(self.put(late, pos(4, 64, 4, "inst-late00000000"))[0], 503)  # not 429
        # Moving inside a full cell and using the next cell both work.
        self.r.clock.advance(1)
        self.assertEqual(self.put(players[0], pos(5, 70, 5, "inst-%012d" % 0))[0], 200)
        status, body, _ = self.put(players[4], pos(40, 64, 4, "inst-next00000000"))
        self.assertEqual((status, len(body["nearby"])), (200, 3))
        # Leaving frees the cell.
        self.assertEqual(self.r.request("DELETE", "/v1/presence", token=players[1], via=A)[0], 204)
        self.assertEqual(self.put(late, pos(4, 64, 4, "inst-late00000000"))[0], 200)

    def test_extreme_and_hostile_coordinates(self):
        token = self.r.create(via=A, capabilities=["presence"])
        for i, (x, y, z) in enumerate([(30_000_000, 4096, -30_000_000), (-30_000_000, -2048, 30_000_000),
                                       (-0.0, 0, 29_999_999.999), (-1e-300, 64, 5e-324)]):
            self.r.clock.advance(1)
            self.assertEqual(self.put(token, pos(x, y, z, "inst-extreme00000"))[0], 200, (x, y, z))
        huge = "1" + "0" * 400
        for field in ("x", "y", "z"):
            self.r.clock.advance(1)
            body = pos(1, 64, 1, "inst-extreme00000")
            raw = ",".join(f'"{k}":{huge if k == field else (v if isinstance(v, (int, float)) else chr(34) + v + chr(34))}'
                           for k, v in body.items())
            for sign in ("", "-"):
                status, resp, _ = self.r.request("PUT", "/v1/presence", token=token, via=A,
                                                 headers={"Content-Type": "application/json"},
                                                 raw_body=("{" + raw.replace(huge, sign + huge) + "}").encode())
                self.assertEqual((status, resp["error"]), (400, "invalid_" + field))
        for bad in ({"x": 30_000_000.001}, {"y": -2049}, {"z": 1e308}, {"y": 2 ** 63}):
            status, _, _ = self.put(token, {**pos(1, 64, 1, "inst-extreme00000"), **bad})
            self.assertEqual(status, 400, bad)
        status, resp, _ = self.r.request("POST", "/v1/sessions", raw_body=b'{"protocol":' + huge.encode() + b"}",
                                         headers={"Content-Type": "application/json"}, via=A)
        self.assertEqual((status, resp["error"]), (400, "unsupported_protocol"))

    def test_global_presence_budget(self):
        r = RunningRelay(proxied(presence_enabled=True, presence_min_interval=0.0,
                                 global_presence_rate=0.01, global_presence_burst=2))
        try:
            tokens = [r.create(via=f"198.18.0.{i}", capabilities=["presence"]) for i in range(3)]
            for i, t in enumerate(tokens[:2]):
                self.assertEqual(r.request("PUT", "/v1/presence", pos(i, 64, 0, "inst-%012d" % i), token=t,
                                           via=f"198.18.0.{i}")[0], 200)
            status, body, headers = r.request("PUT", "/v1/presence", pos(9, 64, 0, "inst-third0000000"),
                                              token=tokens[2], via="198.18.0.2")
            self.assertEqual((status, body["error"]), (503, "overloaded"))
            self.assertIn("retry-after", headers)
            # Heartbeats still pass: presence has its own budget.
            self.assertEqual(r.request("PUT", "/v1/session", {}, token=tokens[2], via="198.18.0.2")[0], 200)
        finally:
            r.close()


class StrictHttpTest(RelayCase):
    config = make_config()

    def test_header_names_and_values(self):
        for line in (b"Bad(Name): 1", b"Bad@Name: 1", b'"Quoted": 1', b"Na\x7fme: 1", b"X: a\x7fb",
                     b"X: a\x0bb", b": empty", b"X Y: 1", b"X\t: 1", b"X\x00: 1"):
            raw = self.r.raw(b"GET /healthz HTTP/1.1\r\n" + line + b"\r\n\r\n")
            self.assertEqual(status_of(raw), 400, line)
        ok = self.r.raw(b"GET /healthz HTTP/1.1\r\nX-Odd_Name.v1~!#$%&'*+^`|: tab\tvalue\r\nConnection: close\r\n\r\n")
        self.assertEqual(status_of(ok), 200)
        for target in (b"/he\x01lthz", b"/healthz\x7f", b"*"):
            self.assertEqual(status_of(self.r.raw(b"GET " + target + b" HTTP/1.1\r\n\r\n")), 400, target)


class ConfigDefaultsTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        base = {"NOCTIS_OPERATOR_NAME": "Op", "NOCTIS_OPERATOR_CONTACT": "op@example.invalid"}
        cfg = load_config(base)
        self.assertEqual(
            (cfg.max_sessions_per_network, cfg.session_binding, cfg.global_request_rate, cfg.global_create_rate,
             cfg.global_presence_rate, cfg.create_prefix_rate, cfg.max_connections_per_peer,
             cfg.max_inflight_per_client, cfg.presence_max_per_cell, cfg.deny_networks),
            (16, "migrate", 2000.0, 50.0, 1000.0, 0.5, 32, 8, 64, ()),
        )
        cfg = load_config({**base, "NOCTIS_DENY_NETWORKS": "203.0.113.0/24 2001:db8::/32", "NOCTIS_SESSION_BINDING": "STRICT",
                           "NOCTIS_MAX_SESSIONS_PER_NETWORK": "0", "NOCTIS_GLOBAL_REQUEST_RATE": "0"})
        self.assertEqual((len(cfg.deny_networks), cfg.session_binding, cfg.max_sessions_per_network), (2, "strict", 0))
        for bad in (
            {**base, "NOCTIS_SESSION_BINDING": "loose"},
            {**base, "NOCTIS_DENY_NETWORKS": "10.0.0.0/33"},
            {**base, "NOCTIS_DENY_FILE": "/nonexistent/deny.txt"},
            {**base, "NOCTIS_TRUSTED_PROXIES": "nope"},
            {**base, "NOCTIS_GLOBAL_CREATE_RATE": "-1"},
            {**base, "NOCTIS_MAX_INFLIGHT_PER_CLIENT": "x"},
        ):
            with self.assertRaises(ConfigError, msg=bad):
                load_config(bad)


if __name__ == "__main__":
    unittest.main()
