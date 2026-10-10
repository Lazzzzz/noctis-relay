import re
import threading
import unittest

from noctis_relay.store import hash_token

from .support import RunningRelay, make_config

HEX64 = re.compile(r"[0-9a-f]{64}")


class SessionApiTest(unittest.TestCase):
    def setUp(self):
        self.r = RunningRelay(make_config())

    def tearDown(self):
        self.r.close()

    def test_healthz(self):
        status, body, headers = self.r.request("GET", "/healthz")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertNotIn("server", headers)

    def test_info_contract(self):
        status, body, _ = self.r.request("GET", "/v1/info")
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], 1)
        self.assertEqual(body["privacyRevision"], 1)
        self.assertEqual(body["retentionSeconds"], 30)
        self.assertEqual(body["heartbeatSeconds"], 10)
        self.assertEqual(body["operator"], {"name": "Test operator", "contact": "relay@example.invalid"})
        self.assertEqual(body["capabilities"], ["sessions"])
        self.assertNotIn("presence", body)

    def test_session_lifecycle(self):
        status, body, _ = self.r.request("POST", "/v1/sessions", {"protocol": 1})
        self.assertEqual(status, 201)
        self.assertEqual(set(body), {"protocol", "token", "expiresInSeconds", "heartbeatSeconds"})
        self.assertEqual((body["protocol"], body["expiresInSeconds"], body["heartbeatSeconds"]), (1, 30, 10))
        token = body["token"]
        self.assertRegex(token, HEX64)

        status, body, _ = self.r.request("PUT", "/v1/session", {}, token=token)
        self.assertEqual((status, body), (200, {"protocol": 1, "expiresInSeconds": 30, "heartbeatSeconds": 10}))

        status, body, _ = self.r.request("DELETE", "/v1/session", token=token)
        self.assertEqual((status, body), (204, None))
        # Idempotent.
        status, _, _ = self.r.request("DELETE", "/v1/session", token=token)
        self.assertEqual(status, 204)
        status, body, _ = self.r.request("PUT", "/v1/session", {}, token=token)
        self.assertEqual((status, body["error"]), (401, "session_expired"))

    def test_tokens_are_unique_and_only_hashed_in_db(self):
        tokens = {self.r.create() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        rows = self.r.call(lambda: self.r.relay.store.db.execute("SELECT * FROM session").fetchall())
        dump = repr(rows)
        for token in tokens:
            self.assertNotIn(token, dump)
            self.assertNotIn(token.encode(), b"".join(bytes(c) for row in rows for c in row if isinstance(c, bytes)))
        hashes = {row[1] for row in rows}
        self.assertEqual(hashes, {hash_token(t) for t in tokens})
        # Only id, token hash, expiry and presence flags: no IP, UUID, name.
        cols = [c[1] for c in self.r.call(lambda: self.r.relay.store.db.execute("PRAGMA table_info(session)").fetchall())]
        self.assertEqual(cols, ["sid", "token_hash", "expires", "presence", "last_presence"])

    def test_expiry_and_heartbeat(self):
        token = self.r.create()
        self.r.clock.advance(25)
        self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=token)[0], 200)
        self.r.clock.advance(25)  # 50 s after creation, 25 s after heartbeat
        self.assertEqual(self.r.request("PUT", "/v1/session", {}, token=token)[0], 200)
        self.r.clock.advance(30.01)
        status, body, headers = self.r.request("PUT", "/v1/session", {}, token=token)
        self.assertEqual((status, body["error"]), (401, "session_expired"))
        self.assertIn("www-authenticate", headers)

    def test_purge_removes_expired_rows(self):
        for _ in range(20):
            self.r.create()
        self.assertEqual(self.r.call(self.r.relay.store.session_count), 20)
        self.r.clock.advance(31)
        removed = self.r.call(lambda: self.r.relay.store.purge(limit=5))
        self.assertEqual(removed, 5)  # bounded batch
        self.r.call(lambda: self.r.relay.store.purge(limit=None))
        self.assertEqual(self.r.call(self.r.relay.store.session_count), 0)
        count = self.r.call(lambda: self.r.relay.store.db.execute("SELECT count(*) FROM session").fetchone()[0])
        self.assertEqual(count, 0)

    def test_auth_errors(self):
        for header in ("", "Bearer", "Bearer abc", "Basic " + "a" * 64, "Bearer " + "A" * 64, "Bearer " + "g" * 64):
            hdrs = {"Authorization": header} if header else {}
            status, body, _ = self.r.request("PUT", "/v1/session", {}, headers=hdrs)
            self.assertEqual((status, body["error"]), (401, "invalid_token"), header)
        status, body, _ = self.r.request("PUT", "/v1/session", {}, token="0" * 64)
        self.assertEqual((status, body["error"]), (401, "session_expired"))
        self.assertEqual(self.r.request("DELETE", "/v1/session")[0], 401)
        # Unknown well-formed token: still 204 (idempotent, reveals nothing).
        self.assertEqual(self.r.request("DELETE", "/v1/session", token="1" * 64)[0], 204)

    def test_bad_session_bodies(self):
        cases = [
            ({}, "invalid_fields"),
            ({"protocol": 2}, "unsupported_protocol"),
            ({"protocol": "1"}, "invalid_protocol"),
            ({"protocol": True}, "invalid_protocol"),
            ({"protocol": 1.0}, "invalid_protocol"),
            ({"protocol": 1, "uuid": "x"}, "invalid_fields"),
            ({"protocol": 1, "capabilities": ["presence"]}, "capability_unavailable"),
            ({"protocol": 1, "capabilities": "presence"}, "invalid_capabilities"),
        ]
        for body, code in cases:
            status, resp, _ = self.r.request("POST", "/v1/sessions", body)
            self.assertEqual((status, resp["error"]), (400, code), body)
        raw_cases = [
            (b"[1]", "invalid_json"),
            (b'"x"', "invalid_json"),
            (b"{", "invalid_json"),
            (b'{"protocol": NaN}', "invalid_json"),
            (b'{"protocol": Infinity}', "invalid_json"),
            (b'{"protocol": 1, "protocol": 1}', "invalid_json"),
            (b"\xff\xfe", "invalid_json"),
        ]
        for raw, code in raw_cases:
            status, resp, _ = self.r.request("POST", "/v1/sessions", raw_body=raw, headers={"Content-Type": "application/json"})
            self.assertEqual((status, resp["error"]), (400, code), raw)
        token = self.r.create()
        status, resp, _ = self.r.request("PUT", "/v1/session", {"x": 1}, token=token)
        self.assertEqual((status, resp["error"]), (400, "invalid_fields"))
        status, resp, _ = self.r.request("PUT", "/v1/session", raw_body=b"", token=token, headers={"Content-Type": "application/json"})
        self.assertEqual((status, resp["error"]), (400, "invalid_json"))

    def test_content_type_and_methods(self):
        status, resp, _ = self.r.request("POST", "/v1/sessions", raw_body=b'{"protocol":1}', headers={"Content-Type": "text/plain"})
        self.assertEqual((status, resp["error"]), (415, "unsupported_media_type"))
        status, _, _ = self.r.request("POST", "/v1/sessions", raw_body=b'{"protocol":1}', headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 201)
        status, resp, headers = self.r.request("GET", "/v1/sessions")
        self.assertEqual((status, headers["allow"]), (405, "POST"))
        self.assertEqual(self.r.request("OPTIONS", "/v1/info")[0], 405)
        self.assertEqual(self.r.request("GET", "/v1/presence")[0], 404)  # capability disabled
        self.assertEqual(self.r.request("GET", "/../etc/passwd")[0], 404)
        self.assertEqual(self.r.request("GET", "/v1/info?redirect=http://evil")[0], 200)

    def test_concurrent_creation_and_heartbeats(self):
        errors = []
        tokens = []
        lock = threading.Lock()

        def worker():
            try:
                for _ in range(20):
                    token = self.r.create()
                    status = self.r.request("PUT", "/v1/session", {}, token=token)[0]
                    if status != 200:
                        errors.append(status)
                    with lock:
                        tokens.append(token)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(tokens)), 320)
        self.assertEqual(self.r.call(self.r.relay.store.session_count), 320)


class QuotaTest(unittest.TestCase):
    def test_session_capacity(self):
        r = RunningRelay(make_config(max_sessions=3))
        try:
            tokens = [r.create() for _ in range(3)]
            status, body, headers = r.request("POST", "/v1/sessions", {"protocol": 1})
            self.assertEqual((status, body["error"]), (503, "capacity"))
            self.assertIn("retry-after", headers)
            r.request("DELETE", "/v1/session", token=tokens[0])
            r.create()
            r.clock.advance(31)  # expired sessions are reclaimed on demand
            r.create()
        finally:
            r.close()

    def test_create_rate_limit(self):
        r = RunningRelay(make_config(create_rate=0.1, create_burst=2))
        try:
            r.create()
            r.create()
            status, body, headers = r.request("POST", "/v1/sessions", {"protocol": 1})
            self.assertEqual((status, body["error"]), (429, "rate_limited"))
            self.assertGreaterEqual(int(headers["retry-after"]), 1)
            r.clock.advance(10)
            r.create()
        finally:
            r.close()

    def test_request_rate_limit(self):
        r = RunningRelay(make_config(request_rate=1.0, request_burst=5))
        try:
            statuses = [r.request("GET", "/healthz")[0] for _ in range(7)]
            self.assertEqual(statuses, [200] * 5 + [429] * 2)
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
