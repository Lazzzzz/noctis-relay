"""Bounded in-memory abuse limits keyed by client network.

Client networks are an IPv4 address or an IPv6 /64. Long-lived structures
(rate tables, concurrency counters, session rows) only ever see a keyed
fingerprint of that network: the key is random per process and never leaves
memory, so nothing here can be turned back into an address after a restart.
Every table is bounded and entries are dropped as soon as they are unused or
evicted in least-recently-used order; nothing is ever swept in full.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from collections import OrderedDict
from typing import Callable, Iterable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
MAX_DENY_ENTRIES = 100_000


def _address(ip) -> IPAddress | None:
    if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        addr = ip
    else:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def client_key(ip) -> str:
    """Group IPv6 clients by /64 (one household or host), IPv4 by address."""
    addr = _address(ip)
    if addr is None:
        return "?"
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((addr, 64), strict=False).network_address) + "/64"
    return str(addr)


def prefix_key(ip) -> str:
    """Coarser grouping (IPv4 /24, IPv6 /48) against cheap address rotation."""
    addr = _address(ip)
    if addr is None:
        return "?"
    bits = 24 if isinstance(addr, ipaddress.IPv4Address) else 48
    return str(ipaddress.ip_network((addr, bits), strict=False))


class Fingerprints:
    """Keyed, truncated hash of a network key (random key per process)."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def tag(self, key: str) -> bytes:
        return hashlib.blake2b(key.encode("ascii"), key=self._key, digest_size=16).digest()


class Bucket:
    """A single token bucket (used for global budgets)."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.burst = float(burst)
        self.clock = clock
        self.tokens = float(burst)
        self.stamp = clock()

    def take(self, cost: float = 1.0) -> float:
        """Consume ``cost`` tokens; return 0 if allowed, else seconds to wait."""
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        return (cost - self.tokens) / self.rate


class TokenBuckets:
    def __init__(self, rate: float, burst: int, capacity: int, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.burst = float(burst)
        self.capacity = capacity
        self.clock = clock
        self._buckets: OrderedDict[object, list[float]] = OrderedDict()

    def take(self, key, cost: float = 1.0) -> float:
        """Consume ``cost`` tokens; return 0 if allowed, else seconds to wait."""
        now = self.clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.capacity:
                self._buckets.popitem(last=False)
            bucket = [self.burst, now]
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)
            bucket[0] = min(self.burst, bucket[0] + (now - bucket[1]) * self.rate)
            bucket[1] = now
        if bucket[0] >= cost:
            bucket[0] -= cost
            return 0.0
        return (cost - bucket[0]) / self.rate

    def refund(self, key, cost: float = 1.0) -> None:
        """Give tokens back when a later check refused the request."""
        bucket = self._buckets.get(key)
        if bucket is not None:
            bucket[0] = min(self.burst, bucket[0] + cost)

    def __len__(self) -> int:
        return len(self._buckets)


class Concurrency:
    """Counts concurrent holders per key; keys disappear when released."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._held: dict[object, int] = {}

    def acquire(self, key) -> bool:
        count = self._held.get(key, 0)
        if self.limit and count >= self.limit:
            return False
        self._held[key] = count + 1
        return True

    def release(self, key) -> None:
        count = self._held.get(key, 0) - 1
        if count > 0:
            self._held[key] = count
        else:
            self._held.pop(key, None)

    def held(self, key) -> int:
        return self._held.get(key, 0)

    def __len__(self) -> int:
        return len(self._held)


def parse_networks(text: str, source: str) -> list:
    """Parse CIDRs or addresses separated by commas, spaces or lines.

    ``#`` starts a comment. Errors report the position only, never the value.
    """
    from .config import ConfigError  # local import: config imports this module

    nets = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for item in re.split(r"[\s,]+", line.split("#", 1)[0]):
            if not item:
                continue
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise ConfigError(f"{source}: invalid network on line {lineno}") from None
            if len(nets) > MAX_DENY_ENTRIES:
                raise ConfigError(f"{source}: more than {MAX_DENY_ENTRIES} networks")
    return nets


class NetworkSet:
    """Membership test against many CIDRs in O(distinct prefix lengths)."""

    def __init__(self, networks: Iterable = ()) -> None:
        tables: dict[tuple[int, int], set[int]] = {}
        count = 0
        for net in networks:
            if isinstance(net, ipaddress.IPv6Network) and net.prefixlen >= 96 and net.network_address.ipv4_mapped:
                net = ipaddress.ip_network((net.network_address.ipv4_mapped, net.prefixlen - 96))
            tables.setdefault((net.version, net.prefixlen), set()).add(int(net.network_address))
            count += 1
        self._v4 = [(32 - plen, vals) for (ver, plen), vals in sorted(tables.items()) if ver == 4]
        self._v6 = [(128 - plen, vals) for (ver, plen), vals in sorted(tables.items()) if ver == 6]
        self.count = count

    def __contains__(self, ip) -> bool:
        addr = _address(ip) if ip is not None else None
        if addr is None:
            return False
        value = int(addr)
        for shift, vals in self._v4 if addr.version == 4 else self._v6:
            if (value >> shift) << shift in vals:
                return True
        return False

    def __len__(self) -> int:
        return self.count
