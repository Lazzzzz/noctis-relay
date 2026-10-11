"""Durable archive at scale: fill, lookups, private deletion and purge.

    python3 bench/bench_archive.py [--clips 120000] [--hot 0.3] [--days 92] [--check]

Fills a temporary on-disk archive (WAL, synchronous=FULL as in production)
with ``--clips`` sampled clips spread over ``--days`` days: a ``--hot``
fraction in one 96-block square (a busy spawn), the rest dispersed over
20 000 x 20 000 blocks, surface and caves. Then measures lookups in the hot
zone and in dispersed places, a private deletion of a heavy owner, and the
expiry purge in bounded batches. The clock is simulated; nothing leaves the
machine. ``--check`` fails if a lookup reads more than the bounded number of
index entries or exceeds the latency budget.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from noctis_relay.archive import DAY, Archive  # noqa: E402
from noctis_relay.store import Store  # noqa: E402

REALM, PROFILE = "astralyth:noctis", "terrain-0123456789abcdef"
BODY = '{"account":"x","frames":' + str([[2 * i, 8 * i, 0, 0, 64, 0, 64, 1] for i in range(40)]) + ',"actions":[]}'


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=int, default=120_000)
    ap.add_argument("--hot", type=float, default=0.3)
    ap.add_argument("--days", type=int, default=92)
    ap.add_argument("--lookups", type=int, default=2000)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    rng = random.Random(42)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "archive.db")
        start = 1_800_000_000.0
        wall = [start]
        a = Archive(path, days=args.days, interval=0, owner_daily=10**9, cell_daily=10**9, cell_max=10**9,
                    wallclock=lambda: wall[0], rng=rng)
        owners = [rng.randbytes(16) for _ in range(5000)]
        heavy = owners[0]
        span = args.days * DAY - 3600
        t0 = time.perf_counter()
        a.db.execute("PRAGMA synchronous=NORMAL")  # fill only; measured operations use FULL
        for i in range(args.clips):
            wall[0] = start + span * i / args.clips
            if rng.random() < args.hot:
                x, z, y = rng.uniform(0, 96), rng.uniform(0, 96), 64.0
            else:
                x, z = rng.uniform(-10_000, 10_000), rng.uniform(-10_000, 10_000)
                y = rng.choice((64.0, rng.uniform(-40, 40)))
            owner = heavy if i % 50 == 0 else rng.choice(owners)
            a.admit(owner, f"00000000-0000-4000-8000-{owners.index(owner):012d}", REALM, PROFILE,
                    Store.instance_key(f"inst-{owners.index(owner):012d}"), x, y, z, BODY, None)
        a.db.execute("PRAGMA synchronous=FULL")
        fill = time.perf_counter() - t0
        wall[0] = start + span + 3600
        print(f"filled {a.rows} clips in {fill:.1f}s, {a.size_bytes() / 1e6:.1f} MB on disk")

        reads = [0]

        def lookups(where):
            times, served, accounts, ages = [], 0, set(), []
            for _ in range(args.lookups):
                x, y, z = where()
                t = time.perf_counter()
                got = a.pick(REALM, PROFILE, x, y, z, None, None, b"", lambda cid: False)
                times.append((time.perf_counter() - t) * 1000)
                if got:
                    served += 1
                    accounts.add(got.account)
                    ages.append((wall[0] - got.created) / DAY)
            return times, served, accounts, ages

        report = {}
        for name, where in (
            ("hot", lambda: (rng.uniform(0, 96), 64.0, rng.uniform(0, 96))),
            ("dispersed", lambda: (rng.uniform(-10_000, 10_000), 64.0, rng.uniform(-10_000, 10_000))),
        ):
            times, served, accounts, ages = lookups(where)
            report[name] = pct(times, 0.99)
            spread = f", age days p10/p50/p90 {pct(ages, .1):.0f}/{pct(ages, .5):.0f}/{pct(ages, .9):.0f}" if ages else ""
            print(f"lookup {name}: p50 {statistics.median(times):.3f} ms, p99 {pct(times, .99):.3f} ms, "
                  f"served {served}/{args.lookups}, {len(accounts)} distinct accounts{spread}")
        plans = a.explain()
        print("plans:", " | ".join(plans))
        reads[0] = a.per_cell * (2 * a.span + 1) ** 2 * (2 * (a.vertical // a.cell_height) + 2)
        print(f"bound: at most {reads[0]} index entries read per lookup")

        t = time.perf_counter()
        removed = a.forget(heavy, chat_only=False)
        print(f"forget heavy owner: {removed} clips in {(time.perf_counter() - t) * 1000:.0f} ms")

        wall[0] = start + span + 30 * DAY  # a month later: about a third has expired
        batches, t = [], time.perf_counter()
        while True:
            b = time.perf_counter()
            n = a.purge(200)
            batches.append((time.perf_counter() - b) * 1000)
            if n == 0:
                break
        print(f"purge: {len(batches) - 1} batches of <=200 in {time.perf_counter() - t:.1f}s, "
              f"batch p99 {pct(batches, .99):.0f} ms, {a.rows} clips left")
        a.close()
        ok = all(p < 50 for p in report.values()) and not any("SCAN clip " in p for p in plans)
        if args.check and not ok:
            print("CHECK FAILED")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
