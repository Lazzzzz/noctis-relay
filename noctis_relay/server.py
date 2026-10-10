"""Minimal, bounded HTTP/1.1 server and the relay API on top of it.

The server is deliberately small and single-threaded (asyncio): every request
is handled on the event loop, which also owns the SQLite connection. It
supports only what the protocol needs: fixed-length JSON bodies, keep-alive,
no chunked encoding, no redirects, no CORS and no access logs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import re
import signal
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from . import PRIVACY_REVISION, PROTOCOL, __version__
from .config import Config, ConfigError, load_config
from .ratelimit import TokenBuckets, client_key
from .store import CapacityError, Session, Store

log = logging.getLogger("noctis_relay")

TOKEN_RE = re.compile(r"[0-9a-f]{64}")
REALM_RE = re.compile(r"[a-z0-9_.-]{1,32}:[a-z0-9_./-]{1,64}")
OPAQUE_RE = re.compile(r"[A-Za-z0-9_-]+")
METHOD_RE = re.compile(r"[A-Z]{1,10}")
MAX_HEADERS = 32
MAX_HEAD_BYTES = 8192
MAX_HORIZONTAL = 30_000_000
MIN_Y, MAX_Y = -2048, 4096
KNOWN_CAPABILITIES = ("presence",)

REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type",
    417: "Expectation Failed", 429: "Too Many Requests", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented", 503: "Service Unavailable",
}


class HttpError(Exception):
    def __init__(self, status: int, code: str, headers: dict | None = None, close: bool = False):
        super().__init__(code)
        self.status = status
        self.code = code
        self.headers = headers or {}
        self.close = close


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    client: str


@dataclass
class Response:
    status: int
    body: dict | None = None
    headers: dict | None = None


# -- strict JSON -------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError("non-finite number")


def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def parse_json_object(req: Request) -> dict:
    ctype = req.headers.get("content-type", "")
    media = ctype.split(";", 1)[0].strip().lower()
    params = ctype.split(";", 1)[1].strip().lower().replace(" ", "") if ";" in ctype else ""
    if media != "application/json" or params not in ("", "charset=utf-8"):
        raise HttpError(415, "unsupported_media_type")
    try:
        obj = json.loads(
            req.body.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HttpError(400, "invalid_json") from None
    if not isinstance(obj, dict):
        raise HttpError(400, "invalid_json")
    return obj


def _only_keys(obj: dict, required: set, optional: set = frozenset()) -> None:
    keys = set(obj)
    if not required <= keys or keys - required - optional:
        raise HttpError(400, "invalid_fields")


def _number(obj: dict, key: str, lo: float, hi: float) -> float:
    value = obj[key]
    # bool is a subclass of int in Python: reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HttpError(400, "invalid_" + key)
    value = float(value)
    if not math.isfinite(value) or not lo <= value <= hi:
        raise HttpError(400, "invalid_" + key)
    return value


def _string(obj: dict, key: str, pattern: re.Pattern, lo: int, hi: int) -> str:
    value = obj[key]
    if not isinstance(value, str) or not lo <= len(value) <= hi or not pattern.fullmatch(value):
        raise HttpError(400, "invalid_" + key)
    return value


def _protocol(obj: dict) -> None:
    value = obj.get("protocol")
    if isinstance(value, bool) or not isinstance(value, int):
        raise HttpError(400, "invalid_protocol")
    if value != PROTOCOL:
        raise HttpError(400, "unsupported_protocol")


# -- API ---------------------------------------------------------------------


class Relay:
    def __init__(self, config: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.store = Store(
            retention_seconds=config.retention_seconds,
            max_sessions=config.max_sessions,
            cell_size=config.cell_size,
            cell_height=config.cell_height,
            near_radius=config.near_radius,
            near_vertical=config.near_vertical,
            near_limit=config.near_limit,
            clock=clock,
        )
        self.requests = TokenBuckets(config.request_rate, config.request_burst, config.rate_table_size, clock)
        self.creates = TokenBuckets(config.create_rate, config.create_burst, config.rate_table_size, clock)
        caps = ["sessions"] + (["presence"] if config.presence_enabled else [])
        self.info = {
            "protocol": PROTOCOL,
            "privacyRevision": PRIVACY_REVISION,
            "retentionSeconds": config.retention_seconds,
            "heartbeatSeconds": config.heartbeat_seconds,
            "operator": {"name": config.operator_name, "contact": config.operator_contact},
            "capabilities": caps,
            "server": {"name": "noctis-relay", "version": __version__},
        }
        if config.presence_enabled:
            self.info["presence"] = {
                "radius": config.near_radius,
                "vertical": config.near_vertical,
                "limit": config.near_limit,
                "minIntervalSeconds": config.presence_min_interval,
            }
        self.routes = {
            "/healthz": {"GET": self.healthz},
            "/v1/info": {"GET": self.get_info},
            "/v1/sessions": {"POST": self.create_session},
            "/v1/session": {"PUT": self.refresh_session, "DELETE": self.delete_session},
        }
        if config.presence_enabled:
            self.routes["/v1/presence"] = {"PUT": self.put_presence, "DELETE": self.delete_presence}

    def handle(self, req: Request) -> Response:
        wait = self.requests.take(req.client)
        if wait:
            raise _too_many(wait)
        methods = self.routes.get(req.path)
        if methods is None:
            raise HttpError(404, "not_found")
        handler = methods.get(req.method)
        if handler is None:
            raise HttpError(405, "method_not_allowed", {"Allow": ", ".join(methods)})
        if req.method in ("GET", "DELETE") and req.body:
            raise HttpError(400, "unexpected_body")
        return handler(req)

    def _session_ttl(self) -> dict:
        return {"expiresInSeconds": self.config.retention_seconds, "heartbeatSeconds": self.config.heartbeat_seconds}

    def _token(self, req: Request) -> str:
        auth = req.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not TOKEN_RE.fullmatch(token):
            raise HttpError(401, "invalid_token", {"WWW-Authenticate": "Bearer"})
        return token

    def _session(self, req: Request) -> tuple[str, Session]:
        token = self._token(req)
        session = self.store.find_session(token)
        if session is None:
            raise HttpError(401, "session_expired", {"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return token, session

    def healthz(self, req: Request) -> Response:
        return Response(200, {"status": "ok"})

    def get_info(self, req: Request) -> Response:
        return Response(200, self.info)

    def create_session(self, req: Request) -> Response:
        obj = parse_json_object(req)
        _only_keys(obj, {"protocol"}, {"capabilities"})
        _protocol(obj)
        presence = False
        if "capabilities" in obj:
            caps = obj["capabilities"]
            if (
                not isinstance(caps, list)
                or len(caps) > len(KNOWN_CAPABILITIES)
                or not all(isinstance(c, str) for c in caps)
                or len(set(caps)) != len(caps)
            ):
                raise HttpError(400, "invalid_capabilities")
            for cap in caps:
                if cap not in KNOWN_CAPABILITIES or not self.config.presence_enabled:
                    raise HttpError(400, "capability_unavailable")
            presence = "presence" in caps
        wait = self.creates.take(req.client)
        if wait:
            raise _too_many(wait)
        try:
            token, _ = self.store.create_session(presence=presence)
        except CapacityError:
            raise HttpError(503, "capacity", {"Retry-After": str(self.config.retention_seconds)}) from None
        body = {"protocol": PROTOCOL, "token": token, **self._session_ttl()}
        if presence:
            body["capabilities"] = ["presence"]
        return Response(201, body)

    def refresh_session(self, req: Request) -> Response:
        token = self._token(req)
        obj = parse_json_object(req)
        _only_keys(obj, set())
        if self.store.refresh_session(token) is None:
            raise HttpError(401, "session_expired", {"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return Response(200, {"protocol": PROTOCOL, **self._session_ttl()})

    def delete_session(self, req: Request) -> Response:
        self.store.delete_session(self._token(req))
        return Response(204)

    def _presence_session(self, req: Request) -> Session:
        _, session = self._session(req)
        if not session.presence:
            raise HttpError(403, "capability_not_granted")
        return session

    def put_presence(self, req: Request) -> Response:
        session = self._presence_session(req)
        obj = parse_json_object(req)
        _only_keys(obj, {"realm", "profile", "instance", "x", "y", "z"})
        realm = _string(obj, "realm", REALM_RE, 3, 97)
        profile = _string(obj, "profile", OPAQUE_RE, 8, 64)
        instance = _string(obj, "instance", OPAQUE_RE, 16, 64)
        x = _number(obj, "x", -MAX_HORIZONTAL, MAX_HORIZONTAL)
        y = _number(obj, "y", MIN_Y, MAX_Y)
        z = _number(obj, "z", -MAX_HORIZONTAL, MAX_HORIZONTAL)
        since = self.clock() - session.last_presence
        if since < self.config.presence_min_interval:
            raise _too_many(self.config.presence_min_interval - since)
        _, nearby = self.store.update_presence(session, realm, profile, instance, x, y, z)
        return Response(200, {
            "expiresInSeconds": self.config.retention_seconds,
            "nearby": [
                {"ref": n.ref, "x": round(n.x, 2), "y": round(n.y, 2), "z": round(n.z, 2),
                 "distance": round(n.distance, 2)}
                for n in nearby
            ],
        })

    def delete_presence(self, req: Request) -> Response:
        self.store.clear_presence(self._presence_session(req))
        return Response(204)

    def purge(self) -> None:
        self.store.purge(limit=self.config.purge_batch)


def _too_many(wait: float) -> HttpError:
    return HttpError(429, "rate_limited", {"Retry-After": str(max(1, math.ceil(wait)))})


# -- HTTP transport ------------------------------------------------------------


def _resolve_client(config: Config, peer: str, headers: dict[str, str]) -> str:
    if config.trust_proxy and "x-forwarded-for" in headers:
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return client_key(peer)
        if any(peer_ip in net for net in config.trusted_proxies):
            # Only the right-most hop was written by our own proxy.
            candidate = headers["x-forwarded-for"].rsplit(",", 1)[-1].strip()
            try:
                return client_key(str(ipaddress.ip_address(candidate)))
            except ValueError:
                pass
    return client_key(peer)


def _parse_head(raw: bytes) -> tuple[str, str, str, dict[str, str]]:
    try:
        text = raw[:-4].decode("ascii")
    except UnicodeDecodeError:
        raise HttpError(400, "bad_request", close=True) from None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not METHOD_RE.fullmatch(parts[0]) or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise HttpError(400, "bad_request", close=True)
    method, target, version = parts
    if not target.startswith("/") or len(target) > 256:
        raise HttpError(400, "bad_request", close=True)
    if len(lines) - 1 > MAX_HEADERS:
        raise HttpError(431, "headers_too_large", close=True)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or any(c in name for c in " \t"):
            raise HttpError(400, "bad_request", close=True)
        name = name.lower()
        value = value.strip(" \t")
        if any(ord(c) < 32 and c != "\t" for c in value):
            raise HttpError(400, "bad_request", close=True)
        if name in headers:
            if name in ("content-length", "authorization", "content-type", "host"):
                raise HttpError(400, "bad_request", close=True)
            headers[name] = headers[name] + ", " + value
        else:
            headers[name] = value
    return method, target.split("?", 1)[0], version, headers


def _log_internal(where: str, exc: BaseException) -> None:
    # Exception type and code locations only: messages may contain request data.
    frames = "".join(traceback.format_tb(exc.__traceback__))
    log.error("internal error in %s: %s\n%s", where, type(exc).__name__, frames)


def _encode(status: int, body: dict | None, extra: dict | None, keep_alive: bool) -> bytes:
    payload = b"" if body is None or status == 204 else json.dumps(body, separators=(",", ":")).encode()
    lines = [f"HTTP/1.1 {status} {REASONS.get(status, 'Unknown')}"]
    if status != 204:
        lines.append("Content-Type: application/json")
        lines.append(f"Content-Length: {len(payload)}")
    lines += [
        "Cache-Control: no-store",
        "X-Content-Type-Options: nosniff",
        "Referrer-Policy: no-referrer",
        "Connection: " + ("keep-alive" if keep_alive else "close"),
    ]
    for name, value in (extra or {}).items():
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload


class HttpServer:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.config = relay.config
        self.active = 0
        self.errors = 0
        self._server: asyncio.base_events.Server | None = None
        self._purger: asyncio.Task | None = None

    async def start(self, host: str | None = None, port: int | None = None) -> int:
        self._server = await asyncio.start_server(
            self._connection,
            host if host is not None else self.config.host,
            self.config.port if port is None else port,
            limit=MAX_HEAD_BYTES,
            backlog=256,
        )
        self._purger = asyncio.create_task(self._purge_loop())
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._purger:
            self._purger.cancel()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.purge_interval)
            try:
                self.relay.purge()
            except Exception as exc:  # keep serving; the next pass retries
                self.errors += 1
                _log_internal("purge", exc)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= self.config.max_connections:
            writer.transport.abort()
            return
        self.active += 1
        try:
            peer = writer.get_extra_info("peername")
            peer_ip = peer[0] if peer else "?"
            for index in range(self.config.max_requests_per_connection):
                if not await self._one_request(reader, writer, peer_ip, index):
                    break
        except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            self.active -= 1
            writer.close()

    async def _send(self, writer, status, body, headers, keep_alive) -> None:
        writer.write(_encode(status, body, headers, keep_alive))
        await asyncio.wait_for(writer.drain(), self.config.body_timeout)

    async def _one_request(self, reader, writer, peer_ip: str, index: int) -> bool:
        cfg = self.config
        timeout = cfg.header_timeout if index == 0 else cfg.keepalive_timeout
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except asyncio.LimitOverrunError:
            await self._send(writer, 431, {"error": "headers_too_large"}, None, False)
            return False
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return False
        try:
            method, path, version, headers = _parse_head(raw)
            if "transfer-encoding" in headers:
                raise HttpError(501, "chunked_not_supported", close=True)
            if "expect" in headers:
                raise HttpError(417, "expectation_failed", close=True)
            length_text = headers.get("content-length", "0")
            if not length_text.isdigit() or len(length_text) > 6:
                raise HttpError(400, "bad_request", close=True)
            length = int(length_text)
            if length > cfg.max_body_bytes:
                raise HttpError(413, "body_too_large", close=True)
            if method in ("POST", "PUT") and "content-length" not in headers:
                raise HttpError(411, "length_required", close=True)
            body = await asyncio.wait_for(reader.readexactly(length), cfg.body_timeout) if length else b""
            keep_alive = version == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
            keep_alive = keep_alive and index + 1 < cfg.max_requests_per_connection
            req = Request(method, path, headers, body, _resolve_client(cfg, peer_ip, headers))
            try:
                resp = self.relay.handle(req)
            except HttpError:
                raise
            except Exception as exc:
                self.errors += 1
                _log_internal("request", exc)
                raise HttpError(500, "internal", close=True) from None
            await self._send(writer, resp.status, resp.body, resp.headers, keep_alive)
            return keep_alive
        except HttpError as err:
            keep_alive = not err.close and index + 1 < cfg.max_requests_per_connection
            await self._send(writer, err.status, {"error": err.code}, err.headers, keep_alive)
            return keep_alive


async def serve(config: Config) -> None:
    server = HttpServer(Relay(config))
    port = await server.start()
    log.info(
        "noctis-relay %s listening on %s:%d (presence=%s, retention=%ss)",
        __version__, config.host, port, "on" if config.presence_enabled else "off", config.retention_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass
    await stop.wait()
    await server.stop()
    log.info("stopped; all sessions discarded")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    asyncio.run(serve(config))
    return 0
