"""Reproducible benchmark of echo clip publication and lookup (no HTTP).

    python3 bench/bench_echoes.py [--players 5000] [--hot 500] [--seed 1]

Prints the lookup query plan, latency percentiles of a clip publication and
of a next-echo lookup (spread and dense zone), delivery-table growth, purge
cost and SQLite page memory.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from noctis_relay.store import AreaFullError, Store  # noqa: E402

REALM = "astralyth:noctis"
PROFILE = "terrain-0123456789abcdef"
# A full-size clip body (60 frames, 8 actions), as stored after validation.
BODY = json.dumps({
    "account": "0f1e2d3c-4b5a-4968-8776-655443322110",
    "frames": [[2 * i, 8 * i, 0, 8 * i, 64, 0, 64, 1] for i in range(60)],
    "actions": [[4 * i, 1, i, 0, i, "minecraft:stone"] for i in range(8)],
}, separators=(",", ":"))


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def fmt(samples):
    us = [v * 1e6 for v in samples]
    return f"p50 {pct(us, 50):7.1f} us  p95 {pct(us, 95):7.1f} us  p99 {pct(us, 99):7.1f} us"


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def where(rng, hot):
    if hot:
        a, d = rng.uniform(0, 2 * math.pi), 64 * math.sqrt(rng.random())
        return d * math.cos(a), rng.uniform(60, 80), d * math.sin(a)
    return rng.uniform(-10_000, 10_000), rng.uniform(40, 120), rng.uniform(-10_000, 10_000)


def run(args):
    rng = random.Random(args.seed)
    clock = Clock()
    store = Store(30, 1_000_000, clock=clock, echo_ttl=60, echo_max_per_cell=args.max_per_cell)
    players = []
    for i in range(args.players):
        _, s = store.create_session(echoes=True)
        players.append([s, "inst-%012d" % i, i < args.hot])
    print(f"SQLite {__import__('sqlite3').sqlite_version}, Python {sys.version.split()[0]}, {sys.platform}")
    print("plan:", " | ".join(store.explain_echo()))

    pub, refused = [], 0
    for p in players:
        x, y, z = where(rng, p[2])
        clock.t += 0.0005
        s0 = time.perf_counter()
        try:
            store.publish_clip(p[0], REALM, PROFILE, p[1], x, y, z, BODY, None, None)
        except AreaFullError:
            refused += 1
        pub.append(time.perf_counter() - s0)
    print(f"\n{len(players)} clip publications ({args.hot} in a 64-block zone, cell cap {args.max_per_cell}):")
    print(f"  publish        {fmt(pub)}  refused (cell full) {refused}")

    spread_lat, hot_lat, found = [], [], 0
    for _ in range(args.lookups):
        p = players[rng.randrange(len(players))]
        x, y, z = where(rng, p[2])
        clock.t += 0.0005
        s0 = time.perf_counter()
        echo = store.next_echo(p[0], REALM, PROFILE, p[1], x, y, z)
        (hot_lat if p[2] else spread_lat).append(time.perf_counter() - s0)
        found += echo is not None
    print(f"{args.lookups} next-echo lookups (query + delivery row): {found} served")
    print(f"  spread         {fmt(spread_lat)}")
    if hot_lat:
        print(f"  dense zone     {fmt(hot_lat)}")
    delivered = store.db.execute("SELECT count(*) FROM delivered").fetchone()[0]
    mem = store.memory_bytes()
    print(f"delivery rows {delivered}; SQLite pages {mem / 1024 / 1024:.2f} MiB "
          f"({mem / max(1, store.clip_count()):.0f} B per clip incl. session and delivery rows)")

    clock.t += 61
    t0 = time.perf_counter()
    batches = 0
    while store.purge(limit=2000):
        batches += 1
    store.purge(limit=None)
    print(f"purge after expiry: {(time.perf_counter() - t0) * 1000:.1f} ms; clips left {store.clip_count()}")
    store.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=5000)
    ap.add_argument("--hot", type=int, default=500)
    ap.add_argument("--lookups", type=int, default=20000)
    ap.add_argument("--max-per-cell", type=int, default=32)
    ap.add_argument("--seed", type=int, default=1)
    run(ap.parse_args())


if __name__ == "__main__":
    main()
