"""Configuration read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .ratelimit import parse_networks


class ConfigError(ValueError):
    pass


def _text(env: Mapping[str, str], key: str, default: str | None, max_len: int) -> str:
    raw = env.get(key, default)
    if raw is None or not raw.strip():
        raise ConfigError(f"{key} is required")
    value = raw.strip()
    if len(value) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(f"{key} must be printable and at most {max_len} characters")
    return value


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw.strip(), 10)
        except ValueError as exc:
            raise ConfigError(f"{key} must be an integer") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return value


def _float(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{key} must be a number") from exc
    if not lo <= value <= hi:  # also rejects NaN
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean (1/0)")


@dataclass(frozen=True)
class Config:
    operator_name: str
    operator_contact: str
    host: str = "127.0.0.1"
    port: int = 8787
    retention_seconds: int = 30
    heartbeat_seconds: int = 10
    max_sessions: int = 10_000
    max_body_bytes: int = 1024
    max_connections: int = 512
    header_timeout: float = 10.0
    body_timeout: float = 10.0
    keepalive_timeout: float = 15.0
    max_requests_per_connection: int = 100
    # Per-client request bucket (all endpoints).
    request_rate: float = 5.0
    request_burst: int = 40
    # Per-client session-creation bucket.
    create_rate: float = 1 / 20
    create_burst: int = 4
    # Session creations per IPv4 /24 or IPv6 /48 (0 = off).
    create_prefix_rate: float = 0.5
    create_prefix_burst: int = 24
    # Live sessions attributed to one client network (0 = off).
    max_sessions_per_network: int = 16
    # "migrate": a session follows the network that last used it (quota
    # included); "strict": it is refused from any other network.
    session_binding: str = "migrate"
    # Requests per live session (all routes). Requests carrying a live
    # session token draw from this instead of the relay-wide request budget,
    # so an anonymous flood cannot starve existing players' heartbeats.
    session_request_rate: float = 2.0
    session_request_burst: int = 20
    # Relay-wide budgets shared by every client (rate 0 = off); the request
    # budget only covers requests without a live session.
    global_request_rate: float = 2000.0
    global_request_burst: int = 4000
    global_create_rate: float = 50.0
    global_create_burst: int = 500
    global_presence_rate: float = 1000.0
    global_presence_burst: int = 2000
    # Open sockets per direct peer network (trusted proxies exempt) and
    # requests in progress per real client network (0 = off).
    max_connections_per_peer: int = 32
    max_inflight_per_client: int = 8
    # Operator block list (403); the file is re-read on SIGHUP.
    deny_networks: tuple = field(default_factory=tuple)
    deny_file: str = ""
    rate_table_size: int = 20_000
    purge_interval: float = 1.0
    purge_batch: int = 2_000
    trust_proxy: bool = False
    trusted_proxies: tuple = field(default_factory=tuple)
    # Optional presence capability (off by default).
    presence_enabled: bool = False
    presence_min_interval: float = 1.0
    cell_size: int = 32
    cell_height: int = 16
    near_radius: int = 48
    near_vertical: int = 12
    near_limit: int = 8
    # Live positions in one spatial cell (0 = off); bounds dense-zone queries.
    presence_max_per_cell: int = 64
    # Optional echoes capability (short replayed movement clips, off by
    # default) and its separate chat sub-capability (off by default).
    echoes_enabled: bool = False
    echo_chat_enabled: bool = False
    echo_ttl: int = 60
    echo_radius: int = 48
    echo_vertical: int = 16
    echo_max_per_cell: int = 32
    echo_max_body_bytes: int = 6144
    echo_publish_interval: float = 20.0
    echo_fetch_interval: float = 20.0
    # Fastest accepted movement in a clip, in blocks per tick.
    echo_max_speed: float = 1.2
    echo_max_vertical_speed: float = 4.0
    global_echo_rate: float = 200.0
    global_echo_burst: int = 400

    def __post_init__(self) -> None:
        if self.heartbeat_seconds * 2 > self.retention_seconds:
            raise ConfigError("NOCTIS_HEARTBEAT_SECONDS must be at most half of NOCTIS_RETENTION_SECONDS")
        if self.session_binding not in ("migrate", "strict"):
            raise ConfigError("NOCTIS_SESSION_BINDING must be migrate or strict")
        if self.echo_chat_enabled and not self.echoes_enabled:
            raise ConfigError("NOCTIS_ECHO_CHAT requires NOCTIS_ECHOES")


def load_deny_file(path: str) -> list:
    """Read an operator block list: one CIDR or address per line, # comments."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(4 * 1024 * 1024)
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"NOCTIS_DENY_FILE: cannot read ({type(exc).__name__})") from None
    return parse_networks(text, "NOCTIS_DENY_FILE")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    proxies = parse_networks(env.get("NOCTIS_TRUSTED_PROXIES", "127.0.0.1/32,::1/128"), "NOCTIS_TRUSTED_PROXIES")
    deny_file = env.get("NOCTIS_DENY_FILE", "").strip()
    load_deny_file(deny_file)  # fail fast at startup
    return Config(
        operator_name=_text(env, "NOCTIS_OPERATOR_NAME", None, 80),
        operator_contact=_text(env, "NOCTIS_OPERATOR_CONTACT", None, 200),
        host=_text(env, "NOCTIS_HOST", "127.0.0.1", 64),
        port=_int(env, "NOCTIS_PORT", 8787, 1, 65535),
        retention_seconds=_int(env, "NOCTIS_RETENTION_SECONDS", 30, 10, 120),
        heartbeat_seconds=_int(env, "NOCTIS_HEARTBEAT_SECONDS", 10, 2, 60),
        max_sessions=_int(env, "NOCTIS_MAX_SESSIONS", 10_000, 1, 1_000_000),
        max_body_bytes=_int(env, "NOCTIS_MAX_BODY_BYTES", 1024, 64, 16_384),
        max_connections=_int(env, "NOCTIS_MAX_CONNECTIONS", 512, 1, 20_000),
        header_timeout=_float(env, "NOCTIS_HEADER_TIMEOUT", 10.0, 0.1, 120.0),
        body_timeout=_float(env, "NOCTIS_BODY_TIMEOUT", 10.0, 0.1, 120.0),
        keepalive_timeout=_float(env, "NOCTIS_KEEPALIVE_TIMEOUT", 15.0, 0.1, 300.0),
        max_requests_per_connection=_int(env, "NOCTIS_MAX_REQUESTS_PER_CONNECTION", 100, 1, 10_000),
        request_rate=_float(env, "NOCTIS_REQUEST_RATE", 5.0, 0.01, 10_000.0),
        request_burst=_int(env, "NOCTIS_REQUEST_BURST", 40, 1, 100_000),
        create_rate=_float(env, "NOCTIS_CREATE_RATE", 1 / 20, 0.001, 10_000.0),
        create_burst=_int(env, "NOCTIS_CREATE_BURST", 4, 1, 100_000),
        create_prefix_rate=_float(env, "NOCTIS_CREATE_PREFIX_RATE", 0.5, 0.0, 10_000.0),
        create_prefix_burst=_int(env, "NOCTIS_CREATE_PREFIX_BURST", 24, 1, 100_000),
        max_sessions_per_network=_int(env, "NOCTIS_MAX_SESSIONS_PER_NETWORK", 16, 0, 1_000_000),
        session_binding=_text(env, "NOCTIS_SESSION_BINDING", "migrate", 16).lower(),
        session_request_rate=_float(env, "NOCTIS_SESSION_REQUEST_RATE", 2.0, 0.0, 10_000.0),
        session_request_burst=_int(env, "NOCTIS_SESSION_REQUEST_BURST", 20, 1, 100_000),
        global_request_rate=_float(env, "NOCTIS_GLOBAL_REQUEST_RATE", 2000.0, 0.0, 1_000_000.0),
        global_request_burst=_int(env, "NOCTIS_GLOBAL_REQUEST_BURST", 4000, 1, 10_000_000),
        global_create_rate=_float(env, "NOCTIS_GLOBAL_CREATE_RATE", 50.0, 0.0, 1_000_000.0),
        global_create_burst=_int(env, "NOCTIS_GLOBAL_CREATE_BURST", 500, 1, 10_000_000),
        global_presence_rate=_float(env, "NOCTIS_GLOBAL_PRESENCE_RATE", 1000.0, 0.0, 1_000_000.0),
        global_presence_burst=_int(env, "NOCTIS_GLOBAL_PRESENCE_BURST", 2000, 1, 10_000_000),
        max_connections_per_peer=_int(env, "NOCTIS_MAX_CONNECTIONS_PER_PEER", 32, 0, 20_000),
        max_inflight_per_client=_int(env, "NOCTIS_MAX_INFLIGHT_PER_CLIENT", 8, 0, 20_000),
        deny_networks=tuple(parse_networks(env.get("NOCTIS_DENY_NETWORKS", ""), "NOCTIS_DENY_NETWORKS")),
        deny_file=deny_file,
        rate_table_size=_int(env, "NOCTIS_RATE_TABLE_SIZE", 20_000, 16, 1_000_000),
        purge_interval=_float(env, "NOCTIS_PURGE_INTERVAL", 1.0, 0.05, 60.0),
        purge_batch=_int(env, "NOCTIS_PURGE_BATCH", 2_000, 10, 1_000_000),
        trust_proxy=_bool(env, "NOCTIS_TRUST_PROXY", False),
        trusted_proxies=tuple(proxies),
        presence_enabled=_bool(env, "NOCTIS_PRESENCE", False),
        presence_min_interval=_float(env, "NOCTIS_PRESENCE_MIN_INTERVAL", 1.0, 0.0, 60.0),
        cell_size=_int(env, "NOCTIS_CELL_SIZE", 32, 4, 1024),
        cell_height=_int(env, "NOCTIS_CELL_HEIGHT", 16, 4, 1024),
        near_radius=_int(env, "NOCTIS_NEAR_RADIUS", 48, 1, 256),
        near_vertical=_int(env, "NOCTIS_NEAR_VERTICAL", 12, 0, 256),
        near_limit=_int(env, "NOCTIS_NEAR_LIMIT", 8, 1, 32),
        presence_max_per_cell=_int(env, "NOCTIS_PRESENCE_MAX_PER_CELL", 64, 0, 100_000),
        echoes_enabled=_bool(env, "NOCTIS_ECHOES", False),
        echo_chat_enabled=_bool(env, "NOCTIS_ECHO_CHAT", False),
        echo_ttl=_int(env, "NOCTIS_ECHO_TTL", 60, 10, 120),
        echo_radius=_int(env, "NOCTIS_ECHO_RADIUS", 48, 8, 128),
        echo_vertical=_int(env, "NOCTIS_ECHO_VERTICAL", 16, 0, 128),
        echo_max_per_cell=_int(env, "NOCTIS_ECHO_MAX_PER_CELL", 32, 0, 100_000),
        echo_max_body_bytes=_int(env, "NOCTIS_ECHO_MAX_BODY_BYTES", 6144, 1024, 16_384),
        echo_publish_interval=_float(env, "NOCTIS_ECHO_PUBLISH_INTERVAL", 20.0, 1.0, 3600.0),
        echo_fetch_interval=_float(env, "NOCTIS_ECHO_FETCH_INTERVAL", 20.0, 1.0, 3600.0),
        echo_max_speed=_float(env, "NOCTIS_ECHO_MAX_SPEED", 1.2, 0.1, 4.0),
        echo_max_vertical_speed=_float(env, "NOCTIS_ECHO_MAX_VERTICAL_SPEED", 4.0, 0.1, 8.0),
        global_echo_rate=_float(env, "NOCTIS_GLOBAL_ECHO_RATE", 200.0, 0.0, 1_000_000.0),
        global_echo_burst=_int(env, "NOCTIS_GLOBAL_ECHO_BURST", 400, 1, 10_000_000),
    )
