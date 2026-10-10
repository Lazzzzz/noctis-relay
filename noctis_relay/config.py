"""Configuration read from environment variables (see .env.example)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Mapping


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

    def __post_init__(self) -> None:
        if self.heartbeat_seconds * 2 > self.retention_seconds:
            raise ConfigError("NOCTIS_HEARTBEAT_SECONDS must be at most half of NOCTIS_RETENTION_SECONDS")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    proxies = []
    for item in env.get("NOCTIS_TRUSTED_PROXIES", "127.0.0.1/32,::1/128").split(","):
        item = item.strip()
        if item:
            try:
                proxies.append(ipaddress.ip_network(item, strict=False))
            except ValueError as exc:
                raise ConfigError(f"NOCTIS_TRUSTED_PROXIES: invalid network {item!r}") from exc
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
    )
