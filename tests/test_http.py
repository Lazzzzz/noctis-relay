import ipaddress
import logging
import socket
import time
import unittest

from noctis_relay.config import ConfigError, load_config
from noctis_relay.ratelimit import client_key
from noctis_relay.server import _resolve_client

from .support import RunningRelay, make_config


def status_of(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1]) if raw else 0


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.r = RunningRelay(make_config(max_body_bytes=256, header_timeout=0.5, keepalive_timeout=0.5, body_timeout=0.5))

    def tearDown(self):
        self.r.close()

    def test_body_too_large(self):
        body = b'{"protocol":1,"pad":"' + b"x" * 300 + b'"}'
        status, resp, headers = self.r.request("POST", "/v1/sessions", raw_body=body, headers={"Content-Type": "application/json"})
        self.assertEqual((status, resp["error"]), (413, "body_too_large"))
        self.assertEqual(headers["connection"], "close")

    def test_chunked_rejected(self):
        raw = self.r.raw(
            b"POST /v1/sessions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\ne\r\n{\"protocol\":1}\r\n0\r\n\r\n"
        )
        self.assertEqual(status_of(raw), 501)

    def test_missing_length(self):
        raw = self.r.raw(b"POST /v1/sessions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\r\n")
        self.assertEqual(status_of(raw), 411)

    def test_conflicting_lengths_and_smuggling(self):
        raw = self.r.raw(
            b"POST /v1/sessions HTTP/1.1\r\nContent-Length: 14\r\nContent-Length: 15\r\n"
            b"Content-Type: application/json\r\n\r\n{\"protocol\":1}"
        )
        self.assertEqual(status_of(raw), 400)
        raw = self.r.raw(b"POST /v1/sessions HTTP/1.1\r\nContent-Length: -1\r\n\r\n")
        self.assertEqual(status_of(raw), 400)
        raw = self.r.raw(b"GET /healthz HTTP/1.1\r\nX: a\r\n folded\r\n\r\n")
        self.assertEqual(status_of(raw), 400)

    def test_header_limits(self):
        many = b"".join(b"X-%d: 1\r\n" % i for i in range(40))
        self.assertEqual(status_of(self.r.raw(b"GET /healthz HTTP/1.1\r\n" + many + b"\r\n")), 431)
        huge = b"GET /healthz HTTP/1.1\r\nX: " + b"a" * 9000 + b"\r\n\r\n"
        self.assertEqual(status_of(self.r.raw(huge)), 431)
        self.assertEqual(status_of(self.r.raw(b"GARBAGE\r\n\r\n")), 400)
        self.assertEqual(status_of(self.r.raw(b"GET http://evil/ HTTP/1.1\r\n\r\n")), 400)
        self.assertEqual(status_of(self.r.raw(b"GET / HTTP/2.0\r\n\r\n")), 400)

    def test_slow_headers_are_cut(self):
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.r.port), timeout=5) as sock:
            sock.sendall(b"GET /healthz HTTP/1.1\r\n")
            data = sock.recv(100)  # server closes after header_timeout
        self.assertEqual(data, b"")
        self.assertLess(time.monotonic() - start, 3)

    def test_slow_body_is_cut(self):
        with socket.create_connection(("127.0.0.1", self.r.port), timeout=5) as sock:
            sock.sendall(b"POST /v1/sessions HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{")
            self.assertEqual(sock.recv(100), b"")

    def test_keep_alive_and_expect(self):
        with socket.create_connection(("127.0.0.1", self.r.port), timeout=5) as sock:
            sock.sendall(b"GET /healthz HTTP/1.1\r\n\r\nGET /healthz HTTP/1.1\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertEqual(data.count(b"HTTP/1.1 200 OK"), 2)
        raw = self.r.raw(b"POST /v1/sessions HTTP/1.1\r\nExpect: 100-continue\r\nContent-Length: 14\r\n\r\n")
        self.assertEqual(status_of(raw), 417)

    def test_no_access_log(self):
        with self.assertNoLogs("noctis_relay", level=logging.DEBUG):
            token = self.r.create()
            self.r.request("PUT", "/v1/session", {}, token=token)
            self.r.request("DELETE", "/v1/session", token=token)
            self.r.request("GET", "/nope")

    def test_internal_error_hides_request_data(self):
        def boom(req):
            raise ValueError("secret " + req.headers.get("authorization", ""))

        self.r.relay.routes["/healthz"]["GET"] = boom
        with self.assertLogs("noctis_relay", level=logging.ERROR) as logs:
            status, body, _ = self.r.request("GET", "/healthz", headers={"Authorization": "Bearer leak-me"})
        self.assertEqual((status, body), (500, {"error": "internal"}))
        self.assertIn("ValueError", logs.output[0])
        self.assertNotIn("leak-me", "".join(logs.output))


class ConnectionCapTest(unittest.TestCase):
    def test_connection_cap(self):
        r = RunningRelay(make_config(max_connections=2, header_timeout=2))
        try:
            idle = [socket.create_connection(("127.0.0.1", r.port)) for _ in range(2)]
            time.sleep(0.2)
            with socket.create_connection(("127.0.0.1", r.port), timeout=2) as extra:
                extra.sendall(b"GET /healthz HTTP/1.1\r\n\r\n")
                try:
                    data = extra.recv(100)
                except ConnectionResetError:
                    data = b""
            self.assertEqual(data, b"")
            for s in idle:
                s.close()
            time.sleep(0.2)
            self.assertEqual(r.request("GET", "/healthz")[0], 200)
        finally:
            r.close()


class ProxyAndConfigTest(unittest.TestCase):
    def test_forwarded_header_ignored_by_default(self):
        cfg = make_config()
        self.assertEqual(_resolve_client(cfg, "203.0.113.5", {"x-forwarded-for": "198.51.100.1"}), "203.0.113.5")

    def test_forwarded_header_from_trusted_proxy(self):
        cfg = make_config(trust_proxy=True, trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),))
        hdr = {"x-forwarded-for": "6.6.6.6, 198.51.100.1"}
        self.assertEqual(_resolve_client(cfg, "127.0.0.1", hdr), "198.51.100.1")
        self.assertEqual(_resolve_client(cfg, "203.0.113.5", hdr), "203.0.113.5")
        self.assertEqual(_resolve_client(cfg, "127.0.0.1", {"x-forwarded-for": "junk"}), "127.0.0.1")

    def test_ipv6_grouping(self):
        self.assertEqual(client_key("2001:db8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        self.assertEqual(client_key("::ffff:192.0.2.1"), "192.0.2.1")

    def test_config_validation(self):
        base = {"NOCTIS_OPERATOR_NAME": "Op", "NOCTIS_OPERATOR_CONTACT": "op@example.invalid"}
        cfg = load_config(base)
        self.assertEqual((cfg.retention_seconds, cfg.heartbeat_seconds, cfg.presence_enabled, cfg.trust_proxy), (30, 10, False, False))
        self.assertEqual(cfg.host, "127.0.0.1")
        for bad in (
            {},
            {**base, "NOCTIS_RETENTION_SECONDS": "121"},
            {**base, "NOCTIS_RETENTION_SECONDS": "20", "NOCTIS_HEARTBEAT_SECONDS": "15"},
            {**base, "NOCTIS_REQUEST_RATE": "nan"},
            {**base, "NOCTIS_OPERATOR_NAME": "bad\nname"},
            {**base, "NOCTIS_PRESENCE": "maybe"},
        ):
            with self.assertRaises(ConfigError, msg=bad):
                load_config(bad)


if __name__ == "__main__":
    unittest.main()
