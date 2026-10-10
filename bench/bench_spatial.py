"""Reproducible benchmark of the in-memory spatial store (no HTTP).

    python3 bench/bench_spatial.py [--players 5000] [--hot 1000] [--seed 1]

Prints the query plan, latency percentiles of a presence update (write +
neighbour query in one transaction), pure query latency, purge cost, SQLite
page memory and the effect of a dense hot zone.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import resource
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from noctis_relay.store import Store  # noqa: E402

REALM = "astralyth:noctis"
PROFILES = ["gen1-profile-a", "gen1-profile-b", "gen1-profile-c", "gen1-profile-d"]


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def fmt(samples):
    us = [v * 1e6 for v in samples]
    return f"p50 {pct(us, 50):7.1f} us  p95 {pct(us, 95):7.1f} us  p99 {pct(us, 99):7.1f} us"


def rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def populate(store, rng, spread, hot, hot_radius=64.0):
    players = []
    for i in range(spread + hot):
        _, s = store.create_session(presence=True)
        if i < spread:
            prof = PROFILES[i % len(PROFILES)]
            x, z = rng.uniform(-10_000, 10_000), rng.uniform(-10_000, 10_000)
            y = rng.choice([rng.uniform(60, 120), rng.uniform(-60, 40)])  # surface or caves
        else:
            prof = PROFILES[0]
            a, d = rng.uniform(0, 2 * math.pi), hot_radius * math.sqrt(rng.random())
            x, z, y = d * math.cos(a), d * math.sin(a), rng.uniform(60, 80)
        inst = "inst-%012d" % i
        store.update_presence(s, REALM, prof, inst, x, y, z)
        players.append([s, prof, inst, x, y, z, i >= spread])
    return players


def run(args):
    rng = random.Random(args.seed)
    clock = Clock()
    store = Store(30, 1_000_000, clock=clock)
    rss_before = rss_mb()

    t0 = time.perf_counter()
    players = populate(store, rng, args.players - args.hot, args.hot)
    load_s = time.perf_counter() - t0
    print(f"SQLite {__import__('sqlite3').sqlite_version}, Python {sys.version.split()[0]}, {sys.platform}")
    print(f"populated {len(players)} presences ({args.hot} in a 64-block hot zone) in {load_s:.2f}s")
    print("plan:", " | ".join(store.explain_near()))

    # Steady state: each update moves a player by a small random step.
    spread_lat, hot_lat, nearby_spread, nearby_hot = [], [], [], []
    changes0 = store.db.total_changes
    t0 = time.perf_counter()
    for _ in range(args.updates):
        clock.t += 0.0005
        p = players[rng.randrange(len(players))]
        p[3] += rng.uniform(-4, 4)
        p[5] += rng.uniform(-4, 4)
        p[4] = min(4096, max(-2048, p[4] + rng.uniform(-1, 1)))
        s0 = time.perf_counter()
        _, near = store.update_presence(p[0], REALM, p[1], p[2], p[3], p[4], p[5])
        dt = time.perf_counter() - s0
        (hot_lat if p[6] else spread_lat).append(dt)
        (nearby_hot if p[6] else nearby_spread).append(len(near))
    total = time.perf_counter() - t0
    rows_changed = store.db.total_changes - changes0
    print(f"\n{args.updates} presence updates (write + neighbour query): {args.updates / total:,.0f} ops/s single thread")
    print(f"  spread players  {fmt(spread_lat)}  avg nearby {statistics.mean(nearby_spread or [0]):.2f}")
    if hot_lat:
        print(f"  hot-zone players {fmt(hot_lat)}  avg nearby {statistics.mean(nearby_hot):.2f} (limit {store.near_limit})")
    print(f"  row changes per update: {rows_changed / args.updates:.2f} (session expiry + presence row/index entries)")

    # Pure read path, for comparison.
    q = []
    for _ in range(2000):
        p = players[rng.randrange(len(players))]
        params = store._near_params(REALM, p[1], p[3], p[4], p[5], clock.t, p[0].sid, b"")
        s0 = time.perf_counter()
        store.db.execute(store._near_sql, params).fetchall()
        q.append(time.perf_counter() - s0)
    print(f"neighbour query only:  {fmt(q)}")

    # Same query without the spatial key (heap table, no index): what each
    # request would cost with a full scan.
    store.db.execute("CREATE TEMP TABLE naive AS SELECT * FROM presence")
    naive_sql = (
        "SELECT sid, x, y, z, (x - ?) * (x - ?) + (z - ?) * (z - ?) AS d2 FROM naive "
        "WHERE realm = ? AND profile = ? AND expires > ? AND sid != ? "
        "AND d2 <= ? AND abs(y - ?) <= ? ORDER BY d2 LIMIT ?"
    )
    n = []
    for _ in range(500):
        p = players[rng.randrange(len(players))]
        params = (p[3], p[3], p[5], p[5], REALM, p[1], clock.t, p[0].sid, 48.0 * 48, p[4], 12.0, 8)
        s0 = time.perf_counter()
        store.db.execute(naive_sql, params).fetchall()
        n.append(time.perf_counter() - s0)
    store.db.execute("DROP TABLE naive")
    print(f"same query, full scan: {fmt(n)}")

    mem = store.memory_bytes()
    print(f"\nSQLite pages: {mem / 1024 / 1024:.2f} MiB ({mem / len(players):.0f} B per player incl. session row)")
    print(f"process peak RSS: {rss_mb():.1f} MiB (before load {rss_before:.1f} MiB)")

    clock.t += 31
    t0 = time.perf_counter()
    batches = 0
    while store.purge(limit=2000):
        batches += 1
    purge_s = time.perf_counter() - t0
    print(f"purge of {len(players)} expired sessions+presences: {purge_s * 1000:.1f} ms in {batches} batches of 2000")
    store.close()

    print("\nhot-zone density (all players inside 64 blocks, one profile):")
    for n in args.density:
        st = Store(30, 1_000_000, clock=clock)
        pl = populate(st, random.Random(args.seed), 0, n)
        lat = []
        for _ in range(1000):
            clock.t += 0.0005
            p = pl[rng.randrange(len(pl))]
            s0 = time.perf_counter()
            st.update_presence(p[0], REALM, p[1], p[2], p[3], p[4], p[5])
            lat.append(time.perf_counter() - s0)
        print(f"  {n:5d} players: {fmt(lat)}")
        st.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=5000)
    ap.add_argument("--hot", type=int, default=500)
    ap.add_argument("--updates", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--density", type=int, nargs="*", default=[50, 200, 1000, 3000])
    run(ap.parse_args())


if __name__ == "__main__":
    main()
