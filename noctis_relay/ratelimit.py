"""Bounded in-memory token buckets keyed by client network.

Client keys only live in process memory, are never logged and are evicted
in least-recently-used order once the table is full.
"""

from __future__ import annotations

import ipaddress
from collections import OrderedDict
from typing import Callable


def client_key(ip: str) -> str:
    """Group IPv6 clients by /64 (one household or host), IPv4 by address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "?"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False).network_address) + "/64"
    return str(addr)


class TokenBuckets:
    def __init__(self, rate: float, burst: int, capacity: int, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.burst = float(burst)
        self.capacity = capacity
        self.clock = clock
        self._buckets: OrderedDict[str, list[float]] = OrderedDict()

    def take(self, key: str, cost: float = 1.0) -> float:
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

    def __len__(self) -> int:
        return len(self._buckets)
