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
        purge_interval=0.05,
    )
    return dataclasses.replace(base, **overrides)


class RunningRelay:
    def __init__(self, config: Config, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.relay = Relay(config, clock=self.clock)
        self.server = HttpServer(self.relay)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.port = self.loop.run_until_complete(self.server.start("127.0.0.1", 0))
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

    def request(self, method, path, body=None, token=None, headers=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
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

    def create(self, **extra):
        status, body, _ = self.request("POST", "/v1/sessions", {"protocol": 1, **extra})
        assert status == 201, (status, body)
        return body["token"]


async def _wrap(fn):
    return fn()
