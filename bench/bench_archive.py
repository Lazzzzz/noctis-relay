"""Durable archive at scale under zone retention: fill, replacement, lookups, deletion, purge.

    python3 bench/bench_archive.py [--days 365] [--sparse 40000] [--legacy 0.1] [--check]

Simulates ``--days`` days with the production defaults (zone quota 4096,
24 clips per zone per day, 92-day floor) in a temporary on-disk archive (WAL,
synchronous=FULL as in production; the fill alone uses NORMAL). A busy
96-block square at the surface (9 zones) receives its 24 clips per zone every
day: each zone fills after about 171 days, then replaces its own oldest clips.
``--sparse`` clips are dispersed over 20 000 x 20 000 blocks, surface and
caves, and are never replaced; a ``--legacy`` fraction of them keeps the fixed
92-day lifetime (schema 1 or unacknowledged notice). 5 000 owners, one heavy
owner with 1 clip in 50.

Measures admission with and without replacement, lookups in the busy square,
in dispersed places and at the oldest sparse clips, a private deletion and
the purge of fixed lifetimes. The clock is simulated; nothing leaves the
machine. ``--check`` fails if a query plan scans, a zone exceeds its quota or
refuses a clip of its daily budget, the oldest sparse clips are not served, or
a lookup p99 exceeds ``--budget``.
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
HOT_ZONES = [(i, j) for i in range(3) for j in range(3)]


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--sparse", type=int, default=40_000)
    ap.add_argument("--legacy", type=float, default=0.1)
    ap.add_argument("--lookups", type=int, default=2000)
    ap.add_argument("--probes", type=int, default=300)
    ap.add_argument("--budget", type=float, default=50.0, help="lookup p99 limit in ms for --check")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    rng = random.Random(42)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "archive.db")
        start = 20_834 * DAY  # a UTC midnight: simulated days are the archive's quota days
        wall = [start]
        # Production retention defaults; owner pacing is lifted to isolate zone behaviour.
        a = Archive(path, interval=0, owner_daily=10**9, wallclock=lambda: wall[0], rng=rng)
        owners = [rng.randbytes(16) for _ in range(5000)]
        heavy = owners[0]
        cy_hot = 64 // a.cell_height

        def admit(i, x, y, z, fixed=False):
            k = 0 if i % 50 == 0 else rng.randrange(1, len(owners))
            return a.admit(owners[k], f"00000000-0000-4000-8000-{k:012d}", REALM, PROFILE,
                           Store.instance_key(f"inst-{k:012d}"), x, y, z, BODY, None, fixed=fixed)

        def hot_place(zone=None):
            i, j = zone or rng.choice(HOT_ZONES)
            return rng.uniform(32 * i, 32 * i + 32), 64.0, rng.uniform(32 * j, 32 * j + 32)

        def sparse_place():
            # Outside the busy zones: a sparse clip there would take a slot of
            # their daily budget (cellDaily, anti-abuse) and blur the zone count.
            while True:
                x, z = rng.uniform(-10_000, 10_000), rng.uniform(-10_000, 10_000)
                y = rng.choice((64.0, rng.uniform(-40, 40)))
                if not (a.cell_of(x, y, z)[2] == cy_hot and 0 <= x < 96 and 0 <= z < 96):
                    return x, y, z

        t0 = time.perf_counter()
        a.db.execute("PRAGMA synchronous=NORMAL")  # fill only; measured operations use FULL
        offered = admitted = n = 0
        probes = []  # the oldest sparse clips with zone retention
        for day in range(args.days):
            # Whole seconds inside the day: no float rounding into the next quota day.
            events = [(rng.randrange(DAY), zone) for zone in HOT_ZONES for _ in range(24)]
            sparse = args.sparse * (day + 1) // args.days - args.sparse * day // args.days
            events += [(rng.randrange(DAY), None) for _ in range(sparse)]
            for when, zone in sorted(events, key=lambda e: e[0]):
                wall[0] = start + day * DAY + when
                n += 1
                if zone is not None:
                    offered += 1
                    admitted += admit(n, *hot_place(zone)) is not None
                else:
                    fixed = rng.random() < args.legacy
                    x, y, z = sparse_place()
                    if admit(n, x, y, z, fixed) is not None and not fixed and len(probes) < args.probes:
                        probes.append((x, y, z))
        a.db.execute("PRAGMA synchronous=FULL")
        fill = time.perf_counter() - t0
        print(f"filled {a.rows} clips over {args.days} days in {fill:.1f}s, {a.size_bytes() / 1e6:.1f} MB on disk; "
              f"busy square: {admitted}/{offered} offered clips admitted")

        hot = a.db.execute(
            "SELECT cx, cz, total FROM cell WHERE realm = ? AND profile = ? AND cy = ? AND cx BETWEEN 0 AND 2 "
            "AND cz BETWEEN 0 AND 2", (REALM, PROFILE, cy_hot)).fetchall()
        oldest = a.db.execute(
            "SELECT min(created), max(created) FROM clip WHERE cy = ? AND cx BETWEEN 0 AND 2 AND cz BETWEEN 0 AND 2",
            (cy_hot,)).fetchone()
        totals = [t for _, _, t in hot]
        print(f"busy zones: totals {min(totals)}..{max(totals)} (quota {a.cell_max}), history kept "
              f"{(wall[0] - oldest[0]) / DAY:.0f} days, newest {(wall[0] - oldest[1]) / 3600:.1f} h old")

        # One more day under synchronous=FULL: replacements in the busy square, sparse admissions.
        replace, plain = [], []
        wall[0] = start + args.days * DAY
        for zone in HOT_ZONES * 24:
            wall[0] += DAY / (24 * len(HOT_ZONES) + 1)
            t = time.perf_counter()
            if admit(1, *hot_place(zone)) is not None:
                replace.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            if admit(1, *sparse_place()) is not None:
                plain.append((time.perf_counter() - t) * 1000)
        print(f"admit with replacement (FULL): p50 {statistics.median(replace):.2f} ms, p99 {pct(replace, .99):.2f} ms, "
              f"{len(replace)} admitted; sparse admit: p50 {statistics.median(plain):.2f} ms, "
              f"p99 {pct(plain, .99):.2f} ms")
        wall[0] += 3600

        def lookups(places):
            times, ages, accounts = [], [], set()
            for x, y, z in places:
                t = time.perf_counter()
                got = a.pick(REALM, PROFILE, x, y, z, None, None, b"", lambda cid: False)
                times.append((time.perf_counter() - t) * 1000)
                if got:
                    ages.append((wall[0] - got.created) / DAY)
                    accounts.add(got.account)
            return times, ages, accounts

        report = {}
        for name, places in (
            ("hot", [(rng.uniform(0, 96), 64.0, rng.uniform(0, 96)) for _ in range(args.lookups)]),
            ("dispersed", [(rng.uniform(-10_000, 10_000), 64.0, rng.uniform(-10_000, 10_000))
                           for _ in range(args.lookups)]),
            ("oldest sparse", probes),
        ):
            times, ages, accounts = lookups(places)
            report[name] = (pct(times, .99), ages, len(places))
            print(f"lookup {name}: p50 {statistics.median(times):.3f} ms, p99 {pct(times, .99):.3f} ms, "
                  f"served {len(ages)}/{len(places)}, {len(accounts)} distinct accounts, "
                  f"age days p10/p50/p90 {pct(ages, .1):.0f}/{pct(ages, .5):.0f}/{pct(ages, .9):.0f}")
        old_served = sum(age > 92 for age in report["oldest sparse"][1])
        print(f"oldest sparse clips: {old_served}/{len(probes)} lookups served a clip older than 92 days")
        plans = a.explain()
        print("plans:", " | ".join(plans))
        bound = a.per_cell * (2 * a.span + 1) ** 2 * (2 * (a.vertical // a.cell_height) + 2)
        print(f"bound: at most {bound} index entries per lookup, 16 per replacement")

        t = time.perf_counter()
        removed = a.forget(heavy, chat_only=False)
        print(f"forget heavy owner: {removed} clips in {(time.perf_counter() - t) * 1000:.0f} ms")

        kept = a.db.execute("SELECT count(*) FROM clip WHERE expires IS NULL").fetchone()[0]
        batches, t = [], time.perf_counter()
        while True:
            b = time.perf_counter()
            n = a.purge(200)
            batches.append((time.perf_counter() - b) * 1000)
            if n == 0:
                break
        untouched = a.db.execute("SELECT count(*) FROM clip WHERE expires IS NULL").fetchone()[0] == kept
        print(f"purge of fixed lifetimes: {len(batches) - 1} batches of <=200 in {time.perf_counter() - t:.1f}s, "
              f"batch p99 {pct(batches, .99):.0f} ms, {a.rows} clips left, zone-retention clips untouched: {untouched}")
        a.close()
        ok = (
            all(p99 < args.budget for p99, _, _ in report.values())
            and not any("SCAN" in p for p in plans)
            and max(totals) <= a.cell_max
            and admitted == offered
            and len(hot) == len(HOT_ZONES)
            and old_served >= 0.9 * len(probes)
            and untouched
        )
        if args.check and not ok:
            print("CHECK FAILED")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
