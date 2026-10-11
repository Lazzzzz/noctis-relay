"""Test harness: runs a real relay on an ephemeral localhost port."""

from __future__ import annotations

import asyncio
import dataclasses
import http.client
import json
import socket
import threading
import time

from noctis_relay.config import Config
from noctis_relay.server import HttpServer, Relay


class FakeClock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def make_config(**overrides) -> Config:
    base = Config(
        operator_name="Test operator",
        operator_contact="relay@example.invalid",
        port=0,
        # Generous limits so functional tests are not throttled; rate-limit
        # tests override them.
        request_rate=10_000.0,
        request_burst=10_000,
        create_rate=10_000.0,
        create_burst=10_000,
        create_prefix_rate=0.0,
        session_request_rate=0.0,
        max_sessions_per_network=0,
        global_request_rate=0.0,
        global_create_rate=0.0,
        global_presence_rate=0.0,
        max_connections_per_peer=0,
        max_inflight_per_client=0,
        presence_max_per_cell=0,
        purge_interval=0.05,
    )
    return dataclasses.replace(base, **overrides)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningRelay:
    def __init__(self, config: Config, clock: FakeClock | None = None, dual_stack: bool = False) -> None:
        """``dual_stack`` also listens on ::1 (same port) for a second real peer."""
        self.clock = clock or FakeClock()
        self.relay = Relay(config, clock=self.clock)
        self.server = HttpServer(self.relay)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        host, port = (["127.0.0.1", "::1"], free_port()) if dual_stack else ("127.0.0.1", 0)

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.port = self.loop.run_until_complete(self.server.start(host, port))
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)

    def call(self, fn):
        """Run ``fn`` on the server loop thread (owner of the SQLite connection)."""
        fut = asyncio.run_coroutine_threadsafe(_wrap(fn), self.loop)
        return fut.result(5)

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        self.relay.store.close()

    # -- client helpers ----------------------------------------------------

    def request(self, method, path, body=None, token=None, headers=None, raw_body=None, via=None, host="127.0.0.1"):
        """``via`` sets X-Forwarded-For, as a trusted proxy would."""
        conn = http.client.HTTPConnection(host, self.port, timeout=5)
        hdrs = dict(headers or {})
        if via is not None:
            hdrs["X-Forwarded-For"] = via
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        if token is not None:
            hdrs["Authorization"] = "Bearer " + token
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        parsed = json.loads(payload) if payload else None
        return resp.status, parsed, dict((k.lower(), v) for k, v in resp.getheaders())

    def raw(self, data: bytes, read_timeout: float = 3.0) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout) as sock:
            sock.sendall(data)
            chunks = []
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
            return b"".join(chunks)

    def create(self, via=None, **extra):
        status, body, _ = self.request("POST", "/v1/sessions", {"protocol": 1, **extra}, via=via)
        assert status == 201, (status, body)
        return body["token"]


async def _wrap(fn):
    return fn()
