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
from .config import Config, ConfigError, load_config, load_deny_file
from .ratelimit import Bucket, Concurrency, Fingerprints, NetworkSet, TokenBuckets, client_key, prefix_key
from .store import AreaFullError, CapacityError, Session, Store

log = logging.getLogger("noctis_relay")

TOKEN_RE = re.compile(r"[0-9a-f]{64}")
REALM_RE = re.compile(r"[a-z0-9_.-]{1,32}:[a-z0-9_./-]{1,64}")
OPAQUE_RE = re.compile(r"[A-Za-z0-9_-]+")
METHOD_RE = re.compile(r"[A-Z]{1,10}")
# RFC 9110 field-name token and request-target without controls or spaces.
HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
TARGET_RE = re.compile(r"/[\x21-\x7e]*")
HEADER_VALUE_BAD_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
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
    # Keyed fingerprints of the client network and of its wider prefix.
    client: bytes
    prefix: bytes = b""


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
    # Compare before converting: float() of a huge JSON integer overflows.
    if isinstance(value, float) and not math.isfinite(value) or not lo <= value <= hi:
        raise HttpError(400, "invalid_" + key)
    return float(value)


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
            max_per_cell=config.presence_max_per_cell,
        )
        self.fingerprints = Fingerprints()
        size = config.rate_table_size
        self.requests = TokenBuckets(config.request_rate, config.request_burst, size, clock)
        self.creates = TokenBuckets(config.create_rate, config.create_burst, size, clock)
        self.prefix_creates = (
            TokenBuckets(config.create_prefix_rate, config.create_prefix_burst, size, clock)
            if config.create_prefix_rate else None
        )
        self.session_requests = (
            TokenBuckets(config.session_request_rate, config.session_request_burst, config.max_sessions, clock)
            if config.session_request_rate else None
        )
        self.global_requests = _bucket(config.global_request_rate, config.global_request_burst, clock)
        self.global_creates = _bucket(config.global_create_rate, config.global_create_burst, clock)
        self.global_presence = _bucket(config.global_presence_rate, config.global_presence_burst, clock)
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

    def admit(self, client: bytes, headers: dict[str, str]) -> None:
        """Request budget, checked from the headers before the body is read.

        The per-client bucket comes first so that a throttled client cannot
        drain the relay-wide budget. A request bearing a live session token
        is then charged to that session (one indexed lookup); only the
        others (session creation, info, unknown tokens) share the relay-wide
        budget, so rotating addresses cannot starve existing sessions.
        """
        wait = self.requests.take(client)
        if wait:
            raise _too_many(wait)
        scheme, _, token = headers.get("authorization", "").partition(" ")
        session = (
            self.store.find_session(token)
            if scheme.lower() == "bearer" and TOKEN_RE.fullmatch(token) else None
        )
        # A replay from another network in strict mode must not spend the
        # legitimate holder's session budget. It is anonymous traffic for
        # admission; the route still rejects it (or permits token revocation).
        if session is None or (
            self.config.session_binding == "strict" and session.net != client
        ):
            _spend(self.global_requests)
        elif self.session_requests is not None:
            wait = self.session_requests.take(session.sid)
            if wait:
                raise _too_many(wait)

    def handle(self, req: Request) -> Response:
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

    def _session(self, req: Request) -> Session:
        session = self.store.find_session(self._token(req))
        if session is None:
            raise _expired()
        return session

    def _check_network(self, req: Request, session: Session) -> None:
        """Refuse, before any change, a session used from another network.

        In ``migrate`` mode the session may follow its holder (Wi-Fi to
        mobile, new DHCP lease) if the new network still has room under its
        live-session cap: a token handed to someone else therefore counts
        against the network that actually uses it. ``strict`` mode answers
        as for an expired session and the client opens a new one.
        """
        if session.net == req.client:
            return
        if self.config.session_binding == "strict":
            raise _expired()
        self._check_network_room(req.client)

    def _check_network_room(self, net: bytes) -> None:
        cap = self.config.max_sessions_per_network
        if cap and self.store.network_sessions(net) >= cap:
            raise HttpError(429, "too_many_sessions", {"Retry-After": str(self.config.retention_seconds)})

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
        # Every refusal below gives the creation tokens back: no session was
        # opened, and a relay-wide shortage must not burn a client's budget.
        taken = []
        try:
            for buckets, key in ((self.creates, req.client), (self.prefix_creates, req.prefix)):
                if buckets is not None:
                    wait = buckets.take(key)
                    if wait:
                        raise _too_many(wait)
                    taken.append((buckets, key))
            self._check_network_room(req.client)
            _spend(self.global_creates)
            try:
                token, _ = self.store.create_session(presence=presence, net=req.client)
            except CapacityError:
                raise HttpError(503, "capacity", {"Retry-After": str(self.config.retention_seconds)}) from None
        except HttpError:
            for buckets, key in taken:
                buckets.refund(key)
            raise
        body = {"protocol": PROTOCOL, "token": token, **self._session_ttl()}
        if presence:
            body["capabilities"] = ["presence"]
        return Response(201, body)

    def refresh_session(self, req: Request) -> Response:
        token = self._token(req)
        obj = parse_json_object(req)
        _only_keys(obj, set())
        session = self.store.find_session(token)
        if session is None:
            raise _expired()
        self._check_network(req, session)
        if self.store.refresh_session(session, req.client) is None:
            raise _expired()
        return Response(200, {"protocol": PROTOCOL, **self._session_ttl()})

    def delete_session(self, req: Request) -> Response:
        # Holding the token is enough to revoke it, from any network: this
        # frees the quota immediately (client disconnect, network change).
        self.store.delete_session(self._token(req))
        return Response(204)

    def _presence_session(self, req: Request) -> Session:
        session = self._session(req)
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
        self._check_network(req, session)
        since = self.clock() - session.last_presence
        if since < self.config.presence_min_interval:
            raise _too_many(self.config.presence_min_interval - since)
        _spend(self.global_presence)
        try:
            _, nearby = self.store.update_presence(session, realm, profile, instance, x, y, z, req.client)
        except AreaFullError:
            raise HttpError(503, "area_full", {"Retry-After": str(self.config.retention_seconds)}) from None
        return Response(200, {
            "expiresInSeconds": self.config.retention_seconds,
            "nearby": [
                {"ref": n.ref, "x": round(n.x, 2), "y": round(n.y, 2), "z": round(n.z, 2),
                 "distance": round(n.distance, 2)}
                for n in nearby
            ],
        })

    def delete_presence(self, req: Request) -> Response:
        # Like DELETE /v1/session, withdrawing a position is always allowed.
        self.store.clear_presence(self._presence_session(req))
        return Response(204)

    def purge(self) -> None:
        self.store.purge(limit=self.config.purge_batch)


def _retry_after(wait: float) -> dict:
    return {"Retry-After": str(max(1, math.ceil(wait)))}


def _too_many(wait: float) -> HttpError:
    return HttpError(429, "rate_limited", _retry_after(wait))


def _expired() -> HttpError:
    return HttpError(401, "session_expired", {"WWW-Authenticate": 'Bearer error="invalid_token"'})


def _bucket(rate: float, burst: int, clock) -> Bucket | None:
    return Bucket(rate, burst, clock) if rate > 0 else None


def _spend(bucket: Bucket | None) -> None:
    """Take one unit from a relay-wide budget; 503 when it is exhausted."""
    if bucket is not None:
        wait = bucket.take()
        if wait:
            raise HttpError(503, "overloaded", _retry_after(wait))


# -- HTTP transport ------------------------------------------------------------


def _peer_address(peer: str):
    try:
        return ipaddress.ip_address(peer)
    except ValueError:
        return None


def _resolve_client(config: Config, peer: str, headers: dict[str, str], proxies: NetworkSet | None = None):
    """Real client address: the peer, or X-Forwarded-For from a trusted proxy."""
    peer_ip = _peer_address(peer)
    if config.trust_proxy and peer_ip is not None and "x-forwarded-for" in headers:
        proxies = NetworkSet(config.trusted_proxies) if proxies is None else proxies
        if peer_ip in proxies:
            # Only the right-most hop was written by our own proxy.
            candidate = headers["x-forwarded-for"].rsplit(",", 1)[-1].strip()
            addr = _peer_address(candidate)
            if addr is not None:
                return addr
    return peer_ip


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
    if len(target) > 256 or not TARGET_RE.fullmatch(target):
        raise HttpError(400, "bad_request", close=True)
    if len(lines) - 1 > MAX_HEADERS:
        raise HttpError(431, "headers_too_large", close=True)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not HEADER_NAME_RE.fullmatch(name):
            raise HttpError(400, "bad_request", close=True)
        name = name.lower()
        value = value.strip(" \t")
        if HEADER_VALUE_BAD_RE.search(value):
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
        self.proxies = NetworkSet(self.config.trusted_proxies if self.config.trust_proxy else ())
        self.deny = self._build_deny()
        # Keys are network fingerprints; entries vanish when released.
        self.peer_sockets = Concurrency(self.config.max_connections_per_peer)
        self.inflight = Concurrency(self.config.max_inflight_per_client)
        self._server: asyncio.base_events.Server | None = None
        self._purger: asyncio.Task | None = None

    def _build_deny(self) -> NetworkSet:
        return NetworkSet([*self.config.deny_networks, *load_deny_file(self.config.deny_file)])

    def reload_deny(self) -> None:
        """Re-read NOCTIS_DENY_FILE (SIGHUP); keep the old list on error."""
        try:
            self.deny = self._build_deny()
        except ConfigError as exc:
            log.error("deny list not reloaded: %s", exc)
            return
        log.info("deny list reloaded: %d networks", len(self.deny))

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
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "?"
        addr = _peer_address(peer_ip)
        # Refused sockets are reset at once, without reading or answering.
        if self.active >= self.config.max_connections or addr in self.deny:
            writer.transport.abort()
            return
        # A trusted proxy multiplexes many players: only the global cap
        # applies to it; its clients are limited per request below.
        slot = None if addr in self.proxies else self.relay.fingerprints.tag(client_key(addr))
        if slot is not None and not self.peer_sockets.acquire(slot):
            writer.transport.abort()
            return
        self.active += 1
        try:
            for index in range(self.config.max_requests_per_connection):
                if not await self._one_request(reader, writer, peer_ip, index):
                    break
        except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            self.active -= 1
            if slot is not None:
                self.peer_sockets.release(slot)
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
        pending = 0  # body bytes announced but not read: the stream is unusable
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
            pending = length
            addr = _resolve_client(cfg, peer_ip, headers, self.proxies)
            if addr in self.deny:
                raise HttpError(403, "forbidden", close=True)
            fingerprints = self.relay.fingerprints
            client = fingerprints.tag(client_key(addr))
            if not self.inflight.acquire(client):
                raise HttpError(429, "concurrency_limited", {"Retry-After": "1"}, close=True)
            try:
                self.relay.admit(client, headers)
                body = await asyncio.wait_for(reader.readexactly(length), cfg.body_timeout) if length else b""
                pending = 0
                keep_alive = version == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
                keep_alive = keep_alive and index + 1 < cfg.max_requests_per_connection
                req = Request(method, path, headers, body, client, fingerprints.tag(prefix_key(addr)))
                try:
                    resp = self.relay.handle(req)
                except HttpError:
                    raise
                except Exception as exc:
                    self.errors += 1
                    _log_internal("request", exc)
                    raise HttpError(500, "internal", close=True) from None
            finally:
                self.inflight.release(client)
            await self._send(writer, resp.status, resp.body, resp.headers, keep_alive)
            return keep_alive
        except HttpError as err:
            keep_alive = not err.close and not pending and index + 1 < cfg.max_requests_per_connection
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
    if hasattr(signal, "SIGHUP"):
        try:
            loop.add_signal_handler(signal.SIGHUP, server.reload_deny)
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
